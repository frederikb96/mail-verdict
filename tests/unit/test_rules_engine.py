"""Tests for RulesEngine: trigger matching, condition evaluation, action execution."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.rules.engine import TRIGGER_TYPES, RulesEngine, _parse_rules
from mail_verdict.rules.executor import ActionExecutor, StopProcessing


class TestTriggerTypes:
    """Tests for TRIGGER_TYPES configuration."""

    def test_all_triggers_present(self) -> None:
        """All expected trigger strings are present."""
        expected = {
            "mail.received",
            "mail.moved",
            "mail.trashed",
            "mail.deleted",
            "flags.changed",
        }
        assert TRIGGER_TYPES == expected


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
    """Tests for RulesEngine PG LISTEN event handling."""

    def _make_engine(
        self,
        rules: list[dict[str, Any]] | None = None,
    ) -> tuple[RulesEngine, MagicMock]:
        """Create a RulesEngine with mock executor."""
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
            action_executor=executor,
        )
        return engine, executor

    @pytest.mark.asyncio
    async def test_handle_insert_event(self) -> None:
        """Insert event triggers matching rules."""
        engine, executor = self._make_engine(rules=[{
            "name": "catch_all",
            "trigger": "mail.received",
            "conditions": {},
            "actions": [{"tag": "test"}],
        }])

        event = {
            "op": "insert",
            "id": str(uuid.uuid4()),
            "account_id": str(uuid.uuid4()),
            "folder_id": str(uuid.uuid4()),
            "folder_name": "INBOX",
        }
        await engine.handle_message_event(event)
        # With no DB, context is empty, so conditions match empty rule
        executor.execute.assert_awaited()

    @pytest.mark.asyncio
    async def test_unrelated_op_ignored(self) -> None:
        """Events with unknown op are ignored."""
        engine, executor = self._make_engine()

        event = {
            "op": "truncate",
            "id": str(uuid.uuid4()),
            "account_id": str(uuid.uuid4()),
            "folder_id": str(uuid.uuid4()),
        }
        await engine.handle_message_event(event)
        executor.execute.assert_not_awaited()

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
        engine, executor = self._make_engine(rules=rules)
        executor.execute = AsyncMock(side_effect=StopProcessing())

        event = {
            "op": "insert",
            "id": str(uuid.uuid4()),
            "account_id": str(uuid.uuid4()),
            "folder_id": str(uuid.uuid4()),
            "folder_name": "INBOX",
        }
        await engine.handle_message_event(event)
        assert executor.execute.await_count == 1
