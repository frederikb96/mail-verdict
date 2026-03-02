"""Tests for RulesEngine: trigger matching, condition evaluation, action execution."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.rules.bus import EventBus
from mail_verdict.rules.engine import TRIGGER_MAP, RulesEngine, _parse_rules
from mail_verdict.rules.executor import ActionExecutor, StopProcessing
from mail_verdict.sync.events import (
    FlagsChanged,
    MailDeleted,
    MailReceived,
    MailSpamDetected,
    MailTrashed,
)


class TestTriggerMap:
    """Tests for TRIGGER_MAP configuration."""

    def test_all_triggers_present(self) -> None:
        """All expected trigger strings are mapped."""
        expected = {
            "mail.received",
            "mail.moved",
            "mail.trashed",
            "mail.spam_detected",
            "mail.deleted",
            "flags.changed",
        }
        assert set(TRIGGER_MAP.keys()) == expected

    def test_mail_received_maps(self) -> None:
        assert TRIGGER_MAP["mail.received"] is MailReceived

    def test_mail_deleted_maps(self) -> None:
        assert TRIGGER_MAP["mail.deleted"] is MailDeleted


class TestParseRules:
    """Tests for _parse_rules."""

    def test_basic_rule(self) -> None:
        """Parses a valid rule dict."""
        raw = [{"name": "test", "trigger": "mail.received", "conditions": {}, "actions": []}]
        rules = _parse_rules(raw)
        assert len(rules) == 1
        assert rules[0].name == "test"
        assert rules[0].trigger == "mail.received"

    def test_unknown_trigger_skipped(self) -> None:
        """Rule with unknown trigger is skipped."""
        raw = [{"name": "bad", "trigger": "unknown.event"}]
        rules = _parse_rules(raw)
        assert len(rules) == 0

    def test_list_conditions_normalized(self) -> None:
        """List of conditions is wrapped in 'all'."""
        raw = [{
            "name": "test",
            "trigger": "mail.received",
            "conditions": [
                {"subject_contains": "invoice"},
                {"sender_match": "example.com"},
            ],
            "actions": [],
        }]
        rules = _parse_rules(raw)
        assert "all" in rules[0].conditions

    def test_single_condition_list(self) -> None:
        """Single-item condition list is unwrapped."""
        raw = [{
            "name": "test",
            "trigger": "mail.received",
            "conditions": [{"subject_contains": "invoice"}],
            "actions": [],
        }]
        rules = _parse_rules(raw)
        assert "subject_contains" in rules[0].conditions

    def test_enrichment_parsed(self) -> None:
        """Enrichment config is parsed from rule."""
        raw = [{
            "name": "test",
            "trigger": "mail.received",
            "enrichment": {"enabled": True, "prompt": "Classify", "tags": ["urgent"]},
            "conditions": {},
            "actions": [],
        }]
        rules = _parse_rules(raw)
        assert rules[0].enrichment.enabled is True
        assert rules[0].enrichment.tags == ["urgent"]


class TestRulesEngine:
    """Tests for RulesEngine event handling."""

    def _make_engine(
        self,
        rules: list[dict[str, Any]] | None = None,
    ) -> tuple[RulesEngine, EventBus, MagicMock]:
        """Create a RulesEngine with mock executor."""
        bus = EventBus()
        executor = MagicMock(spec=ActionExecutor)
        executor.execute = AsyncMock()

        if rules is None:
            rules = [{
                "name": "test_rule",
                "trigger": "mail.received",
                "conditions": {"subject_contains": "invoice"},
                "actions": [{"tag": "billing"}],
            }]

        engine = RulesEngine(
            rules=rules,
            bus=bus,
            action_executor=executor,
        )
        return engine, bus, executor

    @pytest.mark.asyncio
    async def test_start_subscribes_to_bus(self) -> None:
        """start() registers with event bus."""
        engine, bus, _ = self._make_engine()
        await engine.start()
        assert await bus.subscriber_count(MailReceived) > 0
        await engine.stop()

    @pytest.mark.asyncio
    async def test_stop_unsubscribes(self) -> None:
        """stop() removes subscriptions."""
        engine, bus, _ = self._make_engine()
        await engine.start()
        await engine.stop()
        assert await bus.subscriber_count(MailReceived) == 0

    @pytest.mark.asyncio
    async def test_stop_action_halts_processing(self) -> None:
        """'stop' action prevents further rules from executing."""
        rules: list[dict[str, Any]] = [
            {
                "name": "stopper",
                "trigger": "mail.received",
                "conditions": {},
                "actions": [{"stop": True}],
            },
            {
                "name": "unreachable",
                "trigger": "mail.received",
                "conditions": {},
                "actions": [{"tag": "test"}],
            },
        ]
        engine, bus, executor = self._make_engine(rules=rules)
        executor.execute = AsyncMock(side_effect=StopProcessing())
        await engine.start()

        event = MailReceived(
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            uid=1,
        )
        await engine._handle_event(event)
        assert executor.execute.await_count == 1
        await engine.stop()
