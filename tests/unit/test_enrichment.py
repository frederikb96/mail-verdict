"""Tests for EnrichmentRunner: live provider dispatch, tag/reasoning validation."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.rules.conditions import MailContext
from mail_verdict.rules.enrichment import (
    EnrichmentConfig,
    EnrichmentRunner,
    _validate_enrichment_shape,
)


class _SettingsStub:
    """Minimal SettingsService stand-in."""

    def __init__(self, ai: dict[str, Any] | None = None) -> None:
        self._ai = ai or {"provider": "openai", "enrichment_model": "gpt-5.4-nano"}
        self._retry = {
            "max_retries": 1, "base_delay_seconds": 0.001,
            "max_delay_seconds": 0.01, "exponential_base": 2.0,
        }

    def get(self, category: str) -> dict[str, Any]:
        return {"ai": self._ai, "retry": self._retry}.get(category, {})


class _CredRepoStub:
    def __init__(self, keys: dict[str, str] | None = None) -> None:
        # `keys or {...}` would treat an explicitly empty dict the same as
        # None and silently fall back to the default keys -- distinguish
        # "not provided" from "provided as empty" instead.
        self._keys = {"anthropic": "sk-ant-test", "openai": "sk-oai-test"} if keys is None else keys

    async def resolve_key(self, provider: str) -> str | None:
        return self._keys.get(provider)


def _openai_response(text: str) -> MagicMock:
    response = MagicMock()
    response.output_text = text
    return response


class TestValidateEnrichmentShape:
    """Tests for the defense-in-depth shape check."""

    def test_valid_shape_passes(self) -> None:
        _validate_enrichment_shape({"tags": ["urgent"], "reasoning": "Marked urgent."})

    def test_non_list_tags_raises(self) -> None:
        with pytest.raises(ValueError, match="tags"):
            _validate_enrichment_shape({"tags": "urgent", "reasoning": "x"})

    def test_missing_reasoning_raises(self) -> None:
        with pytest.raises(ValueError, match="reasoning"):
            _validate_enrichment_shape({"tags": []})


class TestEnrichmentRunnerDisabled:
    """A rule with enrichment disabled or no tags never calls a provider."""

    @pytest.mark.asyncio
    async def test_disabled_skips_call(self) -> None:
        runner = EnrichmentRunner(_SettingsStub(), _CredRepoStub())
        result = await runner.run(EnrichmentConfig(enabled=False), MailContext())
        assert result.success is True
        assert result.tags == []

    @pytest.mark.asyncio
    async def test_no_tags_skips_call(self) -> None:
        runner = EnrichmentRunner(_SettingsStub(), _CredRepoStub())
        result = await runner.run(
            EnrichmentConfig(enabled=True, tags=[]), MailContext(),
        )
        assert result.success is True


class TestEnrichmentRunnerLiveDispatch:
    """The runner reads ai settings fresh on every run(), like the spam analyst."""

    @pytest.mark.asyncio
    async def test_openai_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import mail_verdict.core.openai_provider as provider_mod

        client = MagicMock()
        client.responses = MagicMock()
        response_json = '{"tags": ["urgent"], "reasoning": "Contains a deadline."}'
        client.responses.create = AsyncMock(return_value=_openai_response(response_json))
        monkeypatch.setattr(provider_mod, "get_openai_client", lambda api_key: client)

        runner = EnrichmentRunner(_SettingsStub(), _CredRepoStub())
        config = EnrichmentConfig(enabled=True, prompt="Classify urgency", tags=["urgent", "low"])
        ctx = MailContext(subject="Deadline today", body_text="Please respond")
        result = await runner.run(config, ctx)

        assert result.success is True
        assert result.tags == ["urgent"]
        assert result.reasoning == "Contains a deadline."

    @pytest.mark.asyncio
    async def test_provider_switch_takes_effect_without_rebuild(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import mail_verdict.core.anthropic_provider as anthropic_mod
        import mail_verdict.core.openai_provider as openai_mod

        anthropic_client = MagicMock()
        anthropic_client.messages = MagicMock()
        anthropic_client.messages.create = AsyncMock(
            side_effect=RuntimeError("must not be called")
        )
        openai_client = MagicMock()
        openai_client.responses = MagicMock()
        response_json = '{"tags": ["urgent"], "reasoning": "Deadline mentioned."}'
        openai_client.responses.create = AsyncMock(return_value=_openai_response(response_json))
        monkeypatch.setattr(anthropic_mod, "get_anthropic_client", lambda api_key: anthropic_client)
        monkeypatch.setattr(openai_mod, "get_openai_client", lambda api_key: openai_client)

        settings = _SettingsStub(ai={"provider": "openai", "enrichment_model": "gpt-5.4-nano"})
        runner = EnrichmentRunner(settings, _CredRepoStub())
        config = EnrichmentConfig(enabled=True, prompt="p", tags=["urgent"])

        result = await runner.run(config, MailContext(subject="s", body_text="b"))
        assert result.success is True
        openai_client.responses.create.assert_awaited_once()
        anthropic_client.messages.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_provider_failure_reports_error_not_raise(self) -> None:
        """A failed enrichment call is a graceful skip for the rule, not a crash."""
        runner = EnrichmentRunner(_SettingsStub(), _CredRepoStub(keys={}))
        config = EnrichmentConfig(enabled=True, prompt="p", tags=["urgent"])
        result = await runner.run(config, MailContext(subject="s", body_text="b"))
        assert result.success is False
        assert result.error
