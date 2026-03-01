"""
E2E: Rules engine pipeline.

Flow: event trigger -> AI enrichment -> condition match -> action execution.
Verifies end-to-end rule evaluation with the event bus, conditions, and actions.

Markers: @pytest.mark.e2e
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import pytest

from mail_verdict.config import DatabaseConfig
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import (
    Account,
    Base,
    Folder,
    Mail,
    SpecialUse,
)
from mail_verdict.database.repository import TagRepository
from mail_verdict.rules.bus import EventBus, Subscriber
from mail_verdict.rules.conditions import MailContext, evaluate_condition
from mail_verdict.rules.engine import RulesEngine
from mail_verdict.rules.enrichment import EnrichmentResult, EnrichmentRunner
from mail_verdict.rules.executor import ActionExecutor, ActionResult, StopProcessing
from mail_verdict.sync.events import (
    MailReceived,
    SyncEvent,
)

TEST_DB_URL = (
    "postgresql+asyncpg://mail_verdict_test:mail_verdict_test@localhost:5433/mail_verdict_test"
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[DatabaseConnection]:
    """Module-scoped database connection."""
    config = DatabaseConfig(url=TEST_DB_URL, pool_size=5, max_overflow=2)
    conn = DatabaseConnection(config)
    await conn.init()
    async with conn.engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    yield conn
    async with conn.engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
    await conn.close()


@pytest.fixture(scope="module")
async def test_data(db: DatabaseConnection) -> dict[str, Any]:
    """Seed base test data."""
    async with db.session() as session:
        acc = Account(
            name="e2e-rules-account",
            imap_host="localhost",
            imap_port=1143,
            imap_user="rules@localhost",
        )
        session.add(acc)
        await session.flush()

        inbox = Folder(
            account_id=acc.id,
            imap_name="INBOX",
            special_use=SpecialUse.INBOX,
        )
        junk = Folder(
            account_id=acc.id,
            imap_name="Junk",
            special_use=SpecialUse.JUNK,
        )
        session.add(inbox)
        session.add(junk)
        await session.flush()

        # A mail to trigger rules against
        mail = Mail(
            account_id=acc.id,
            folder_id=inbox.id,
            uid=8001,
            subject="Invoice #2024-001 from Acme Corp",
            from_addr="billing@acme-corp.com",
            body_text="Please review the attached invoice for Q4 2024 services.",
            received_at=datetime.now(timezone.utc),
            size_bytes=1024,
        )
        session.add(mail)
        await session.flush()
        await session.refresh(mail)

        return {
            "account_id": acc.id,
            "inbox": inbox,
            "junk": junk,
            "mail": mail,
            "mail_id": mail.id,
        }


class TestEventBusDispatch:
    """Test event bus dispatching to rules engine."""

    async def test_event_triggers_matching_rule(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """
        MailReceived event triggers rules with 'mail.received' trigger.
        """
        received_events: list[SyncEvent] = []

        async def capture_handler(event: SyncEvent) -> None:
            received_events.append(event)

        bus = EventBus()
        await bus.subscribe(
            MailReceived,
            Subscriber(name="test_capture", callback=capture_handler, priority=10),
        )

        event = MailReceived(
            account_id=test_data["account_id"],
            folder_id=test_data["inbox"].id,
            uid=8001,
        )
        await bus.emit(event)

        assert len(received_events) == 1
        assert isinstance(received_events[0], MailReceived)
        assert received_events[0].uid == 8001

    async def test_priority_ordering(self) -> None:
        """
        Subscribers are dispatched in priority order (lower first).
        """
        call_order: list[str] = []

        async def low_priority(event: SyncEvent) -> None:
            call_order.append("low")

        async def high_priority(event: SyncEvent) -> None:
            call_order.append("high")

        bus = EventBus()
        # Subscribe low priority first, high priority second
        await bus.subscribe(
            MailReceived,
            Subscriber(name="low", callback=low_priority, priority=90),
        )
        await bus.subscribe(
            MailReceived,
            Subscriber(name="high", callback=high_priority, priority=10),
        )

        event = MailReceived(
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            uid=1,
        )
        await bus.emit(event)

        assert call_order == ["high", "low"]

    async def test_subscriber_exception_does_not_stop_others(self) -> None:
        """
        A failing subscriber does not prevent other subscribers from running.
        """
        call_order: list[str] = []

        async def failing_handler(event: SyncEvent) -> None:
            call_order.append("failing")
            raise RuntimeError("Simulated failure")

        async def success_handler(event: SyncEvent) -> None:
            call_order.append("success")

        bus = EventBus()
        await bus.subscribe(
            MailReceived,
            Subscriber(name="failing", callback=failing_handler, priority=10),
        )
        await bus.subscribe(
            MailReceived,
            Subscriber(name="success", callback=success_handler, priority=20),
        )

        event = MailReceived(
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            uid=1,
        )
        await bus.emit(event)

        assert "failing" in call_order
        assert "success" in call_order


class TestConditionEvaluation:
    """Test condition matching against mail context."""

    def test_subject_contains_match(self) -> None:
        """subject_contains matches case-insensitively."""
        ctx = MailContext(subject="Invoice #2024-001 from Acme Corp")
        assert evaluate_condition({"subject_contains": "invoice"}, ctx) is True
        assert evaluate_condition({"subject_contains": "receipt"}, ctx) is False

    def test_sender_domain_match(self) -> None:
        """sender_domain checks the domain part."""
        ctx = MailContext(from_addr="billing@acme-corp.com")
        assert evaluate_condition({"sender_domain": "acme-corp.com"}, ctx) is True
        assert evaluate_condition({"sender_domain": "other.com"}, ctx) is False

    def test_size_threshold(self) -> None:
        """size_gt and size_lt threshold checks."""
        ctx = MailContext(size_bytes=5_000_000)
        assert evaluate_condition({"size_gt": 1_000_000}, ctx) is True
        assert evaluate_condition({"size_gt": 10_000_000}, ctx) is False
        assert evaluate_condition({"size_lt": 10_000_000}, ctx) is True

    def test_composite_all(self) -> None:
        """all: [...] requires every sub-condition to match."""
        ctx = MailContext(
            subject="Invoice from Acme",
            from_addr="billing@acme.com",
        )
        condition = {
            "all": [
                {"subject_contains": "invoice"},
                {"sender_domain": "acme.com"},
            ]
        }
        assert evaluate_condition(condition, ctx) is True

        # One sub-condition fails
        condition_fail = {
            "all": [
                {"subject_contains": "invoice"},
                {"sender_domain": "other.com"},
            ]
        }
        assert evaluate_condition(condition_fail, ctx) is False

    def test_composite_any(self) -> None:
        """any: [...] requires at least one sub-condition to match."""
        ctx = MailContext(subject="Meeting Notes")
        condition = {
            "any": [
                {"subject_contains": "invoice"},
                {"subject_contains": "meeting"},
            ]
        }
        assert evaluate_condition(condition, ctx) is True

    def test_composite_not(self) -> None:
        """not: {...} negates the inner condition."""
        ctx = MailContext(from_addr="trusted@company.com")
        condition = {"not": {"sender_domain": "spam.com"}}
        assert evaluate_condition(condition, ctx) is True

    def test_enrichment_tag_condition(self) -> None:
        """enrichment_tag checks tags from AI enrichment."""
        ctx = MailContext(enrichment_tags=["financial", "urgent"])
        assert evaluate_condition({"enrichment_tag": "financial"}, ctx) is True
        assert evaluate_condition({"enrichment_tag": "social"}, ctx) is False

    def test_has_attachment_filter(self) -> None:
        """has_attachment with optional type filter."""
        ctx = MailContext(
            has_attachments=True,
            attachment_types=["application/pdf", "image/png"],
        )
        assert evaluate_condition({"has_attachment": True}, ctx) is True
        assert evaluate_condition({"has_attachment": "application/pdf"}, ctx) is True
        assert evaluate_condition({"has_attachment": "application/zip"}, ctx) is False

    def test_regex_subject_matches(self) -> None:
        """subject_matches uses regex."""
        ctx = MailContext(subject="Order Confirmation #12345")
        assert evaluate_condition({"subject_matches": r"#\d{5}"}, ctx) is True
        assert evaluate_condition({"subject_matches": r"^SPAM:"}, ctx) is False


class TestEnrichmentIntegration:
    """Test AI enrichment in the rules pipeline."""

    async def test_enrichment_tags_flow_to_conditions(self) -> None:
        """
        Enrichment tags are injected into context and available for condition evaluation.
        """
        # Mock enrichment runner that returns tags
        mock_runner = AsyncMock(spec=EnrichmentRunner)
        mock_runner.run.return_value = EnrichmentResult(
            tags=["financial", "invoice"],
            reasoning="Contains billing keywords",
            success=True,
        )

        # Mock action executor
        mock_executor = AsyncMock(spec=ActionExecutor)
        mock_executor.execute.return_value = ActionResult(
            action_type="tag",
            success=True,
        )

        rule_config = {
            "name": "tag-invoices",
            "trigger": "mail.received",
            "enrichment": {
                "enabled": True,
                "prompt": "Classify this email",
                "tags": ["financial", "invoice", "social", "newsletter"],
            },
            "conditions": {"enrichment_tag": "financial"},
            "actions": [{"tag": "needs-review"}],
        }

        bus = EventBus()
        engine = RulesEngine(
            rules=[rule_config],
            bus=bus,
            action_executor=mock_executor,
            enrichment_runner=mock_runner,
        )
        await engine.start()

        event = MailReceived(
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            uid=100,
        )
        await bus.emit(event)

        # Enrichment was called
        mock_runner.run.assert_called_once()
        # Action was executed because enrichment tag matched condition
        mock_executor.execute.assert_called_once()

        await engine.stop()

    async def test_enrichment_failure_skips_rule(self) -> None:
        """
        When enrichment fails, the rule is skipped (no actions executed).
        """
        mock_runner = AsyncMock(spec=EnrichmentRunner)
        mock_runner.run.return_value = EnrichmentResult(
            success=False,
            error="LLM timeout",
        )

        mock_executor = AsyncMock(spec=ActionExecutor)

        rule_config = {
            "name": "failing-enrichment",
            "trigger": "mail.received",
            "enrichment": {
                "enabled": True,
                "prompt": "Classify this email",
                "tags": ["urgent"],
            },
            "conditions": {"enrichment_tag": "urgent"},
            "actions": [{"tag": "needs-attention"}],
        }

        bus = EventBus()
        engine = RulesEngine(
            rules=[rule_config],
            bus=bus,
            action_executor=mock_executor,
            enrichment_runner=mock_runner,
        )
        await engine.start()

        event = MailReceived(
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            uid=200,
        )
        await bus.emit(event)

        mock_runner.run.assert_called_once()
        mock_executor.execute.assert_not_called()

        await engine.stop()


class TestActionExecution:
    """Test rule action execution."""

    async def test_move_action_calls_propagator(self) -> None:
        """
        move_to action delegates to ActionPropagator.
        """
        mock_propagator = AsyncMock()
        mock_propagator.execute_imap.return_value = True

        executor = ActionExecutor(propagator=mock_propagator)
        ctx = MailContext(folder="INBOX")

        result = await executor.execute(
            {"move_to": "Archive"},
            ctx,
            mail_id=uuid.uuid4(),
            uid=100,
        )
        assert result.success is True
        assert result.action_type == "move_to"
        mock_propagator.execute_imap.assert_called_once()

    async def test_tag_action_persists_to_db(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """
        tag action writes to Postgres via TagRepository.
        """
        tag_repo = TagRepository(db)
        mock_propagator = AsyncMock()
        mock_propagator.execute_imap.return_value = True

        executor = ActionExecutor(
            propagator=mock_propagator,
            tag_repo=tag_repo,
        )
        ctx = MailContext(folder="INBOX")

        result = await executor.execute(
            {"tag": "processed"},
            ctx,
            mail_id=test_data["mail_id"],
            uid=8001,
        )
        assert result.success is True

        # Verify tag was persisted
        tags = await tag_repo.get_tags(test_data["mail_id"])
        tag_names = [t.tag_name for t in tags]
        assert "processed" in tag_names

    async def test_forward_action_delegates(self) -> None:
        """
        forward_to action delegates to ActionPropagator.execute_forward.
        """
        mock_propagator = AsyncMock()
        mock_propagator.execute_forward.return_value = True

        executor = ActionExecutor(propagator=mock_propagator)
        ctx = MailContext(
            folder="INBOX",
            subject="Important Notice",
            from_addr="sender@example.com",
        )

        result = await executor.execute(
            {
                "forward_to": {
                    "address": "admin@company.com",
                    "subject_rewrite": "[FWD] {subject} from {from}",
                }
            },
            ctx,
            mail_id=uuid.uuid4(),
            uid=500,
        )
        assert result.success is True

        call_args = mock_propagator.execute_forward.call_args
        fwd_action = call_args.args[0]
        assert fwd_action.to_address == "admin@company.com"
        assert "Important Notice" in fwd_action.subject_template
        assert "sender@example.com" in fwd_action.subject_template

    async def test_stop_action_halts_processing(self) -> None:
        """
        stop action raises StopProcessing to halt further rules.
        """
        executor = ActionExecutor()
        ctx = MailContext()

        with pytest.raises(StopProcessing):
            await executor.execute(
                {"stop": True},
                ctx,
                mail_id=uuid.uuid4(),
                uid=1,
            )

    async def test_notify_action(self) -> None:
        """
        notify action calls the notify callback with rendered template.
        """
        notify_calls: list[tuple[str, uuid.UUID | None]] = []

        async def notify_cb(msg: str, *, mail_id: uuid.UUID | None = None) -> None:
            notify_calls.append((msg, mail_id))

        executor = ActionExecutor(notify_callback=notify_cb)
        ctx = MailContext(
            subject="Test Subject",
            from_addr="sender@example.com",
        )
        mid = uuid.uuid4()

        result = await executor.execute(
            {"notify": "New mail from {from}: {subject}"},
            ctx,
            mail_id=mid,
            uid=1,
        )
        assert result.success is True
        assert len(notify_calls) == 1
        assert "sender@example.com" in notify_calls[0][0]
        assert "Test Subject" in notify_calls[0][0]


class TestFullRulePipeline:
    """Test the complete rule evaluation pipeline."""

    async def test_event_to_enrichment_to_condition_to_action(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """
        Full pipeline: MailReceived -> enrichment -> condition match -> tag action.
        """
        mock_runner = AsyncMock(spec=EnrichmentRunner)
        mock_runner.run.return_value = EnrichmentResult(
            tags=["invoice"],
            reasoning="Email contains invoice keywords",
            success=True,
        )

        mock_executor = AsyncMock(spec=ActionExecutor)
        mock_executor.execute.return_value = ActionResult(
            action_type="tag",
            success=True,
        )

        rules = [
            {
                "name": "auto-tag-invoices",
                "trigger": "mail.received",
                "enrichment": {
                    "enabled": True,
                    "prompt": "Tag invoices",
                    "tags": ["invoice", "receipt", "newsletter"],
                },
                "conditions": {"enrichment_tag": "invoice"},
                "actions": [
                    {"tag": "billing"},
                    {"notify": "Invoice received from {from}"},
                ],
            },
        ]

        bus = EventBus()
        engine = RulesEngine(
            rules=rules,
            bus=bus,
            action_executor=mock_executor,
            enrichment_runner=mock_runner,
            db=db,
        )
        await engine.start()

        event = MailReceived(
            account_id=test_data["account_id"],
            folder_id=test_data["inbox"].id,
            uid=8001,
        )
        await bus.emit(event)

        # Both actions executed
        assert mock_executor.execute.call_count == 2

        await engine.stop()

    async def test_multiple_rules_stop_processing(self) -> None:
        """
        When a rule has a 'stop' action, subsequent rules are not evaluated.
        """
        mock_executor = AsyncMock(spec=ActionExecutor)

        call_count = 0

        async def tracked_execute(action, ctx, **kwargs):
            nonlocal call_count
            call_count += 1
            if action.get("stop"):
                raise StopProcessing()
            return ActionResult(action_type="tag", success=True)

        mock_executor.execute = tracked_execute

        rules = [
            {
                "name": "first-rule",
                "trigger": "mail.received",
                "enrichment": {},
                "conditions": {},
                "actions": [
                    {"tag": "matched"},
                    {"stop": True},
                ],
            },
            {
                "name": "second-rule-never-reached",
                "trigger": "mail.received",
                "enrichment": {},
                "conditions": {},
                "actions": [{"tag": "should-not-happen"}],
            },
        ]

        bus = EventBus()
        engine = RulesEngine(
            rules=rules,
            bus=bus,
            action_executor=mock_executor,
        )
        await engine.start()

        event = MailReceived(
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            uid=300,
        )
        await bus.emit(event)

        # First rule ran tag + stop, second rule never reached
        assert call_count == 2  # tag + stop from first rule

        await engine.stop()

    async def test_condition_mismatch_skips_actions(self) -> None:
        """
        When conditions don't match, no actions are executed.
        """
        mock_executor = AsyncMock(spec=ActionExecutor)

        rules = [
            {
                "name": "conditional-rule",
                "trigger": "mail.received",
                "enrichment": {},
                "conditions": {"sender_domain": "very-specific-domain-that-wont-match.xyz"},
                "actions": [{"tag": "should-not-happen"}],
            },
        ]

        bus = EventBus()
        engine = RulesEngine(
            rules=rules,
            bus=bus,
            action_executor=mock_executor,
        )
        await engine.start()

        event = MailReceived(
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            uid=400,
        )
        await bus.emit(event)

        mock_executor.execute.assert_not_called()

        await engine.stop()

    async def test_unknown_trigger_skipped(self) -> None:
        """
        Rules with unknown trigger strings are silently skipped during parsing.
        """
        mock_executor = AsyncMock(spec=ActionExecutor)

        rules = [
            {
                "name": "bad-trigger",
                "trigger": "nonexistent.event",
                "enrichment": {},
                "conditions": {},
                "actions": [{"tag": "unreachable"}],
            },
        ]

        bus = EventBus()
        engine = RulesEngine(
            rules=rules,
            bus=bus,
            action_executor=mock_executor,
        )
        await engine.start()

        # No subscriptions made for unknown trigger
        count = await bus.subscriber_count(MailReceived)
        assert count == 0

        await engine.stop()
