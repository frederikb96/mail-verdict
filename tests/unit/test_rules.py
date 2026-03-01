"""
Unit tests for rules engine: triggers, conditions, actions, AI enrichment.

Row 152 (o=10.44): event matching, condition composition, condition types,
action types, enrichment, stop action.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from mail_verdict.rules.conditions import (
    ConditionEvaluator,
    MailContext,
    evaluate_condition,
)
from mail_verdict.rules.engine import (
    TRIGGER_MAP,
    _parse_rules,
)
from mail_verdict.rules.enrichment import (
    EnrichmentConfig,
    EnrichmentRunner,
)
from mail_verdict.rules.executor import (
    ActionExecutor,
    StopProcessing,
    _render_template,
)

pytestmark = pytest.mark.unit


# ===========================================================================
# Condition evaluator tests
# ===========================================================================


class TestConditionEvaluator:
    """Tests for individual condition types."""

    @pytest.fixture
    def evaluator(self) -> ConditionEvaluator:
        return ConditionEvaluator()

    @pytest.fixture
    def ctx(self, make_mail_context: Any) -> MailContext:
        return make_mail_context(
            subject="Invoice #12345 from Acme Corp",
            body_text="Please pay the attached invoice by January 31st.",
            from_addr="billing@acme.com",
            to_addrs=["user@example.com"],
            raw_headers={"X-Priority": "1", "List-Unsubscribe": "<mailto:unsub@acme.com>"},
            size_bytes=15000,
            has_attachments=True,
            attachment_types=["application/pdf"],
            folder="INBOX",
            tags=["important"],
        )

    def test_subject_contains_match(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        assert evaluator.evaluate({"subject_contains": "Invoice"}, ctx) is True

    def test_subject_contains_case_insensitive(
        self, evaluator: ConditionEvaluator, ctx: MailContext
    ) -> None:
        assert evaluator.evaluate({"subject_contains": "invoice"}, ctx) is True

    def test_subject_contains_no_match(
        self, evaluator: ConditionEvaluator, ctx: MailContext
    ) -> None:
        assert evaluator.evaluate({"subject_contains": "receipt"}, ctx) is False

    def test_body_contains_match(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        assert evaluator.evaluate({"body_contains": "January 31st"}, ctx) is True

    def test_body_contains_no_match(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        assert evaluator.evaluate({"body_contains": "February"}, ctx) is False

    def test_subject_matches_regex(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        assert evaluator.evaluate({"subject_matches": r"Invoice #\d+"}, ctx) is True

    def test_subject_matches_no_match(
        self, evaluator: ConditionEvaluator, ctx: MailContext
    ) -> None:
        assert evaluator.evaluate({"subject_matches": r"^Receipt"}, ctx) is False

    def test_subject_matches_invalid_regex(
        self, evaluator: ConditionEvaluator, ctx: MailContext
    ) -> None:
        assert evaluator.evaluate({"subject_matches": "[invalid"}, ctx) is False

    def test_body_matches_regex(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        assert evaluator.evaluate({"body_matches": r"pay.*invoice"}, ctx) is True

    def test_sender_match_email(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        assert evaluator.evaluate({"sender_match": "billing@acme.com"}, ctx) is True

    def test_sender_match_domain(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        assert evaluator.evaluate({"sender_match": "acme.com"}, ctx) is True

    def test_sender_match_no_match(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        assert evaluator.evaluate({"sender_match": "other.com"}, ctx) is False

    def test_sender_domain(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        assert evaluator.evaluate({"sender_domain": "acme.com"}, ctx) is True

    def test_sender_domain_no_match(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        assert evaluator.evaluate({"sender_domain": "other.com"}, ctx) is False

    def test_header_match(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        cond = {"header_match": {"field": "X-Priority", "pattern": "^1$"}}
        assert evaluator.evaluate(cond, ctx) is True

    def test_header_exists(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        assert evaluator.evaluate({"header_exists": "List-Unsubscribe"}, ctx) is True
        assert evaluator.evaluate({"header_exists": "X-Nonexistent"}, ctx) is False

    def test_size_gt(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        assert evaluator.evaluate({"size_gt": 10000}, ctx) is True
        assert evaluator.evaluate({"size_gt": 20000}, ctx) is False

    def test_size_lt(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        assert evaluator.evaluate({"size_lt": 20000}, ctx) is True
        assert evaluator.evaluate({"size_lt": 10000}, ctx) is False

    def test_has_attachment_bool(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        assert evaluator.evaluate({"has_attachment": True}, ctx) is True

    def test_has_attachment_type_filter(
        self, evaluator: ConditionEvaluator, ctx: MailContext
    ) -> None:
        assert evaluator.evaluate({"has_attachment": "application/pdf"}, ctx) is True
        assert evaluator.evaluate({"has_attachment": "image/png"}, ctx) is False

    def test_folder_is(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        assert evaluator.evaluate({"folder_is": "INBOX"}, ctx) is True
        assert evaluator.evaluate({"folder_is": "Sent"}, ctx) is False

    def test_tag_is(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        assert evaluator.evaluate({"tag_is": "important"}, ctx) is True
        assert evaluator.evaluate({"tag_is": "urgent"}, ctx) is False

    def test_enrichment_tag(self, evaluator: ConditionEvaluator, ctx: MailContext) -> None:
        ctx.enrichment_tags = ["newsletter", "promotional"]
        assert evaluator.evaluate({"enrichment_tag": "newsletter"}, ctx) is True
        assert evaluator.evaluate({"enrichment_tag": "spam"}, ctx) is False

    def test_unknown_condition_returns_false(
        self, evaluator: ConditionEvaluator, ctx: MailContext
    ) -> None:
        assert evaluator.evaluate({"nonexistent_condition": "value"}, ctx) is False

    def test_empty_condition_returns_false(
        self, evaluator: ConditionEvaluator, ctx: MailContext
    ) -> None:
        assert evaluator.evaluate({}, ctx) is False


# ===========================================================================
# Condition composition tests
# ===========================================================================


class TestConditionComposition:
    """Tests for all/any/not condition composition."""

    @pytest.fixture
    def ctx(self, make_mail_context: Any) -> MailContext:
        return make_mail_context(
            subject="Test Invoice",
            from_addr="billing@acme.com",
            folder="INBOX",
        )

    def test_all_composition(self, ctx: MailContext) -> None:
        """all: requires every sub-condition to match."""
        condition = {
            "all": [
                {"subject_contains": "Invoice"},
                {"sender_domain": "acme.com"},
            ]
        }
        assert evaluate_condition(condition, ctx) is True

    def test_all_one_fails(self, ctx: MailContext) -> None:
        """all: fails if any sub-condition fails."""
        condition = {
            "all": [
                {"subject_contains": "Invoice"},
                {"sender_domain": "other.com"},
            ]
        }
        assert evaluate_condition(condition, ctx) is False

    def test_any_composition(self, ctx: MailContext) -> None:
        """any: requires at least one sub-condition to match."""
        condition = {
            "any": [
                {"subject_contains": "Receipt"},
                {"sender_domain": "acme.com"},
            ]
        }
        assert evaluate_condition(condition, ctx) is True

    def test_any_none_match(self, ctx: MailContext) -> None:
        """any: fails if no sub-condition matches."""
        condition = {
            "any": [
                {"subject_contains": "Receipt"},
                {"sender_domain": "other.com"},
            ]
        }
        assert evaluate_condition(condition, ctx) is False

    def test_not_composition(self, ctx: MailContext) -> None:
        """not: negates the inner condition."""
        condition = {"not": {"sender_domain": "other.com"}}
        assert evaluate_condition(condition, ctx) is True

    def test_not_negates_match(self, ctx: MailContext) -> None:
        """not: negates a matching condition."""
        condition = {"not": {"sender_domain": "acme.com"}}
        assert evaluate_condition(condition, ctx) is False

    def test_nested_composition(self, ctx: MailContext) -> None:
        """Deeply nested composition works correctly."""
        condition = {
            "all": [
                {"subject_contains": "Invoice"},
                {"not": {"sender_domain": "evil.com"}},
                {
                    "any": [
                        {"folder_is": "INBOX"},
                        {"folder_is": "Sent"},
                    ]
                },
            ]
        }
        assert evaluate_condition(condition, ctx) is True


# ===========================================================================
# Action executor tests
# ===========================================================================


class TestActionExecutor:
    """Tests for each action type."""

    @pytest.fixture
    def executor(self, mock_action_propagator: AsyncMock) -> ActionExecutor:
        return ActionExecutor(
            propagator=mock_action_propagator,
            tag_repo=AsyncMock(),
            notify_callback=AsyncMock(),
        )

    @pytest.fixture
    def ctx(self, make_mail_context: Any) -> MailContext:
        return make_mail_context(folder="INBOX")

    @pytest.mark.asyncio(loop_scope="function")
    async def test_move_to(self, executor: ActionExecutor, ctx: MailContext) -> None:
        result = await executor.execute({"move_to": "Archive"}, ctx, uid=42)
        assert result.action_type == "move_to"
        assert result.success is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_copy_to(self, executor: ActionExecutor, ctx: MailContext) -> None:
        result = await executor.execute({"copy_to": "Backup"}, ctx, uid=42)
        assert result.action_type == "copy_to"
        assert result.success is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_mark_as_read(self, executor: ActionExecutor, ctx: MailContext) -> None:
        result = await executor.execute({"mark_as": "read"}, ctx, uid=42)
        assert result.success is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_star(self, executor: ActionExecutor, ctx: MailContext) -> None:
        result = await executor.execute({"star": True}, ctx, uid=42)
        assert result.success is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_tag(
        self, executor: ActionExecutor, ctx: MailContext, sample_mail_id: Any
    ) -> None:
        result = await executor.execute(
            {"tag": "important"},
            ctx,
            mail_id=sample_mail_id,
            uid=42,
        )
        assert result.success is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_trash(self, executor: ActionExecutor, ctx: MailContext) -> None:
        result = await executor.execute({"trash": True}, ctx, uid=42)
        assert result.success is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_stop_raises(self, executor: ActionExecutor, ctx: MailContext) -> None:
        with pytest.raises(StopProcessing):
            await executor.execute({"stop": True}, ctx)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_unknown_action(self, executor: ActionExecutor, ctx: MailContext) -> None:
        result = await executor.execute({"nonexistent_action": True}, ctx)
        assert result.success is False
        assert result.error == "Unknown action"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_notify(self, executor: ActionExecutor, ctx: MailContext) -> None:
        result = await executor.execute(
            {"notify": "New mail from {from}"},
            ctx,
            uid=42,
        )
        assert result.success is True


class TestRenderTemplate:
    """Tests for template rendering in actions."""

    def test_basic_template(self, make_mail_context: Any) -> None:
        ctx = make_mail_context(
            from_addr="sender@test.com",
            subject="Hello World",
        )
        rendered = _render_template("From: {from}, Subject: {subject}", ctx)
        assert rendered == "From: sender@test.com, Subject: Hello World"

    def test_missing_variable_preserved(self, make_mail_context: Any) -> None:
        ctx = make_mail_context()
        rendered = _render_template("Value: {unknown}", ctx)
        assert rendered == "Value: {unknown}"


# ===========================================================================
# Enrichment tests
# ===========================================================================


class TestEnrichmentRunner:
    """Tests for per-rule AI enrichment."""

    @pytest.fixture
    def runner(self) -> EnrichmentRunner:
        return EnrichmentRunner(
            ai_provider="openai",
            ai_model="gpt-4o-mini",
            max_retries=1,
            excerpt_length=300,
        )

    @pytest.mark.asyncio(loop_scope="function")
    async def test_disabled_enrichment(self, runner: EnrichmentRunner) -> None:
        """Disabled enrichment returns success with empty tags."""
        config = EnrichmentConfig(enabled=False, prompt="test", tags=["a"])
        ctx = MailContext(subject="Test", body_text="Test body", from_addr="a@b.com")
        result = await runner.run(config, ctx)
        assert result.success is True
        assert result.tags == []

    @pytest.mark.asyncio(loop_scope="function")
    async def test_empty_tags_returns_success(self, runner: EnrichmentRunner) -> None:
        """Enrichment with no allowed tags returns success."""
        config = EnrichmentConfig(enabled=True, prompt="test", tags=[])
        ctx = MailContext(subject="Test", body_text="body", from_addr="a@b.com")
        result = await runner.run(config, ctx)
        assert result.success is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_parse_and_validate_valid(self, runner: EnrichmentRunner) -> None:
        """Valid LLM output parses correctly."""
        result = runner._parse_and_validate(
            '{"tags": ["newsletter", "promo"], "reasoning": "looks like marketing"}',
            allowed_tags=["newsletter", "promo", "urgent"],
        )
        assert result.success is True
        assert result.tags == ["newsletter", "promo"]
        assert result.reasoning == "looks like marketing"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_parse_and_validate_filters_invalid_tags(self, runner: EnrichmentRunner) -> None:
        """Tags not in allowed list are filtered out."""
        result = runner._parse_and_validate(
            '{"tags": ["newsletter", "spam"], "reasoning": "test"}',
            allowed_tags=["newsletter", "promo"],
        )
        assert result.tags == ["newsletter"]

    @pytest.mark.asyncio(loop_scope="function")
    async def test_parse_and_validate_markdown_fences(self, runner: EnrichmentRunner) -> None:
        """Handles markdown code fences in LLM output."""
        raw = '```json\n{"tags": ["newsletter"], "reasoning": "test"}\n```'
        result = runner._parse_and_validate(raw, allowed_tags=["newsletter"])
        assert result.tags == ["newsletter"]

    def test_parse_and_validate_missing_tags_key(self, runner: EnrichmentRunner) -> None:
        """Missing tags key raises KeyError."""
        with pytest.raises(KeyError):
            runner._parse_and_validate('{"reasoning": "test"}', allowed_tags=["a"])

    def test_parse_and_validate_invalid_json(self, runner: EnrichmentRunner) -> None:
        """Invalid JSON raises JSONDecodeError."""
        with pytest.raises(json.JSONDecodeError):
            runner._parse_and_validate("not json", allowed_tags=["a"])


# ===========================================================================
# Rule parsing / trigger mapping tests
# ===========================================================================


class TestRuleParsing:
    """Tests for _parse_rules and TRIGGER_MAP."""

    def test_trigger_map_coverage(self) -> None:
        """All documented triggers are in TRIGGER_MAP."""
        expected = {
            "mail.received",
            "mail.moved",
            "mail.trashed",
            "mail.spam_detected",
            "mail.deleted",
            "flags.changed",
        }
        assert set(TRIGGER_MAP.keys()) == expected

    def test_parse_valid_rule(self) -> None:
        """Valid rule YAML parses into RuleConfig."""
        raw = [
            {
                "name": "test-rule",
                "trigger": "mail.received",
                "conditions": {"subject_contains": "Invoice"},
                "actions": [{"move_to": "Archive"}],
            }
        ]
        rules = _parse_rules(raw)
        assert len(rules) == 1
        assert rules[0].name == "test-rule"
        assert rules[0].trigger == "mail.received"

    def test_parse_unknown_trigger_skipped(self) -> None:
        """Rules with unknown triggers are skipped."""
        raw = [
            {
                "name": "bad-rule",
                "trigger": "nonexistent.event",
                "conditions": {},
                "actions": [],
            }
        ]
        rules = _parse_rules(raw)
        assert len(rules) == 0

    def test_parse_list_conditions_normalized(self) -> None:
        """List of conditions is normalized to {all: [...]}."""
        raw = [
            {
                "name": "multi-cond",
                "trigger": "mail.received",
                "conditions": [
                    {"subject_contains": "A"},
                    {"sender_domain": "b.com"},
                ],
                "actions": [],
            }
        ]
        rules = _parse_rules(raw)
        assert "all" in rules[0].conditions

    def test_parse_single_condition_list(self) -> None:
        """Single-item condition list is unwrapped."""
        raw = [
            {
                "name": "single",
                "trigger": "mail.received",
                "conditions": [{"subject_contains": "A"}],
                "actions": [],
            }
        ]
        rules = _parse_rules(raw)
        assert rules[0].conditions == {"subject_contains": "A"}

    def test_parse_enrichment(self) -> None:
        """Enrichment config is parsed from rule YAML."""
        raw = [
            {
                "name": "enriched",
                "trigger": "mail.received",
                "conditions": {},
                "actions": [],
                "enrichment": {
                    "enabled": True,
                    "prompt": "Classify this email",
                    "tags": ["newsletter", "spam"],
                },
            }
        ]
        rules = _parse_rules(raw)
        assert rules[0].enrichment.enabled is True
        assert rules[0].enrichment.tags == ["newsletter", "spam"]
