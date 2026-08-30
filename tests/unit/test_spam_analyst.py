"""Tests for SpamAnalyst: verdict parsing, prompt building, live provider dispatch."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.core.errors import ProviderUnavailableError
from mail_verdict.spam.analyst import (
    AnalysisContext,
    FakeSpamAnalyst,
    LiveSpamAnalyst,
    SpamVerdict,
    _auth_str,
    _build_user_prompt,
    _looks_like_one_sentence,
    _validate_spam_shape,
)


class _SettingsStub:
    """Minimal SettingsService stand-in: mutable ai/spam/retry dicts."""

    def __init__(self, ai: dict[str, Any] | None = None) -> None:
        self._ai = ai or {"provider": "openai", "model": "gpt-5.4-nano", "max_tokens": 256}
        self._retry = {
            "max_retries": 1, "base_delay_seconds": 0.001,
            "max_delay_seconds": 0.01, "exponential_base": 2.0,
        }

    def get(self, category: str) -> dict[str, Any]:
        return {"ai": self._ai, "retry": self._retry}.get(category, {})

    def set_provider(self, provider: str) -> None:
        self._ai = {**self._ai, "provider": provider}


class _CredRepoStub:
    """Minimal ProviderCredentialRepository stand-in."""

    def __init__(self, keys: dict[str, str] | None = None) -> None:
        # `keys or {...}` would treat an explicitly empty dict the same as
        # None and silently fall back to the default keys -- distinguish
        # "not provided" from "provided as empty" instead.
        self._keys = {"anthropic": "sk-ant-test", "openai": "sk-oai-test"} if keys is None else keys

    async def resolve_key(self, provider: str) -> str | None:
        return self._keys.get(provider)


def _anthropic_response(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


def _openai_response(text: str) -> MagicMock:
    response = MagicMock()
    response.output_text = text
    return response


class TestAuthStr:
    """Tests for _auth_str helper."""

    def test_pass(self) -> None:
        assert _auth_str(True) == "pass"

    def test_fail(self) -> None:
        assert _auth_str(False) == "fail"

    def test_unknown(self) -> None:
        assert _auth_str(None) == "unknown"


class TestLooksLikeOneSentence:
    """Tests for the reasoning single-sentence heuristic."""

    def test_single_sentence_passes(self) -> None:
        assert _looks_like_one_sentence("This looks like a phishing attempt.") is True

    def test_no_terminator_passes(self) -> None:
        assert _looks_like_one_sentence("no punctuation here") is True

    def test_two_sentences_fails(self) -> None:
        assert _looks_like_one_sentence("First sentence. Second sentence.") is False


class TestValidateSpamShape:
    """Tests for _validate_spam_shape -- the retry-triggering schema check."""

    def test_valid_shape_passes(self) -> None:
        _validate_spam_shape({"verdict": "spam", "reasoning": "Looks like phishing."})

    def test_invalid_verdict_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid verdict"):
            _validate_spam_shape({"verdict": "maybe", "reasoning": "x"})

    def test_missing_reasoning_raises(self) -> None:
        with pytest.raises(ValueError, match="reasoning"):
            _validate_spam_shape({"verdict": "spam"})

    def test_multi_sentence_reasoning_raises(self) -> None:
        with pytest.raises(ValueError, match="single sentence"):
            _validate_spam_shape({"verdict": "spam", "reasoning": "One. Two."})


class TestBuildUserPrompt:
    """Tests for _build_user_prompt."""

    def test_contains_email_content_tags(self) -> None:
        ctx = AnalysisContext(
            mail_id="m-1", from_addr="alice@example.com", to_addrs="bob@example.com",
            subject="Test", body_excerpt="Hello",
        )
        prompt = _build_user_prompt(ctx)
        assert "<email_content>" in prompt
        assert "</email_content>" in prompt

    def test_truncates_long_content(self) -> None:
        ctx = AnalysisContext(
            mail_id="m-1", from_addr=None, to_addrs=None, subject=None,
            body_excerpt="x" * 20000,
        )
        prompt = _build_user_prompt(ctx)
        assert "[truncated]" in prompt


class TestAnalysisContext:
    """Tests for AnalysisContext serialization."""

    def test_to_dict_structure(self) -> None:
        ctx = AnalysisContext(
            mail_id="m-1", from_addr="alice@example.com", to_addrs="bob@example.com",
            subject="Test", body_excerpt="Body", dkim_pass=True, spf_pass=False, dmarc_pass=None,
        )
        d = ctx.to_dict()
        assert d["new_mail"]["from"] == "alice@example.com"
        assert d["new_mail"]["auth"]["dkim"] == "pass"
        assert d["new_mail"]["auth"]["spf"] == "fail"
        assert d["new_mail"]["auth"]["dmarc"] == "unknown"


def _make_context() -> AnalysisContext:
    return AnalysisContext(
        mail_id="m-1", from_addr="test@example.com", to_addrs="bob@example.com",
        subject="Test", body_excerpt="Hello",
    )


class TestLiveSpamAnalystAnthropic:
    """Tests for LiveSpamAnalyst dispatching to the Anthropic provider."""

    def _patch_client(self, monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> None:
        import mail_verdict.core.anthropic_provider as provider_mod

        monkeypatch.setattr(provider_mod, "get_anthropic_client", lambda api_key: client)

    @pytest.mark.asyncio
    async def test_successful_analysis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = MagicMock()
        client.messages = MagicMock()
        response_json = '{"verdict": "not-spam", "reasoning": "Looks routine."}'
        client.messages.create = AsyncMock(return_value=_anthropic_response(response_json))
        self._patch_client(monkeypatch, client)

        analyst = LiveSpamAnalyst(
            _SettingsStub(ai={"provider": "anthropic", "model": "claude-haiku-4-5"}),
            _CredRepoStub(),
        )
        verdict = await analyst.analyze(_make_context())
        assert isinstance(verdict, SpamVerdict)
        assert verdict.is_spam is False
        assert verdict.reasoning == "Looks routine."

        # A json_schema output_config was passed, not a loose JSON-object mode.
        _, kwargs = client.messages.create.call_args
        assert kwargs["output_config"]["format"]["type"] == "json_schema"

    @pytest.mark.asyncio
    async def test_retries_on_malformed_reasoning_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A multi-sentence reasoning field is a schema violation, retried."""
        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = AsyncMock(
            side_effect=[
                _anthropic_response('{"verdict": "spam", "reasoning": "One. Two."}'),
                _anthropic_response('{"verdict": "spam", "reasoning": "Phishing attempt."}'),
            ]
        )
        self._patch_client(monkeypatch, client)

        analyst = LiveSpamAnalyst(
            _SettingsStub(ai={"provider": "anthropic", "model": "claude-haiku-4-5"}),
            _CredRepoStub(),
        )
        verdict = await analyst.analyze(_make_context())
        assert verdict.is_spam is True
        assert client.messages.create.await_count == 2

    @pytest.mark.asyncio
    async def test_no_key_raises_provider_unavailable(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        analyst = LiveSpamAnalyst(
            _SettingsStub(ai={"provider": "anthropic", "model": "claude-haiku-4-5"}),
            _CredRepoStub(keys={}),
        )
        with pytest.raises(ProviderUnavailableError):
            await analyst.analyze(_make_context())


class TestLiveSpamAnalystOpenAI:
    """Tests for LiveSpamAnalyst dispatching to the OpenAI provider."""

    def _patch_client(self, monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> None:
        import mail_verdict.core.openai_provider as provider_mod

        monkeypatch.setattr(provider_mod, "get_openai_client", lambda api_key: client)

    @pytest.mark.asyncio
    async def test_successful_analysis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = MagicMock()
        client.responses = MagicMock()
        response_json = '{"verdict": "spam", "reasoning": "Classic lottery scam."}'
        client.responses.create = AsyncMock(return_value=_openai_response(response_json))
        self._patch_client(monkeypatch, client)

        ai_settings = {"provider": "openai", "model": "gpt-5.4-nano", "reasoning_effort": "low"}
        analyst = LiveSpamAnalyst(_SettingsStub(ai=ai_settings), _CredRepoStub())
        verdict = await analyst.analyze(_make_context())
        assert verdict.is_spam is True
        assert verdict.reasoning == "Classic lottery scam."

        _, kwargs = client.responses.create.call_args
        assert kwargs["text"]["format"]["strict"] is True
        assert kwargs["reasoning"] == {"effort": "low"}

    @pytest.mark.asyncio
    async def test_exhausts_retries_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = MagicMock()
        client.responses = MagicMock()
        client.responses.create = AsyncMock(return_value=_openai_response("not json"))
        self._patch_client(monkeypatch, client)

        analyst = LiveSpamAnalyst(
            _SettingsStub(ai={"provider": "openai", "model": "gpt-5.4-nano"}),
            _CredRepoStub(),
        )
        with pytest.raises(RuntimeError, match="failed after"):
            await analyst.analyze(_make_context())


class TestLiveSpamAnalystProviderSwitch:
    """The default analyst instance never changes -- only the settings it reads do."""

    @pytest.mark.asyncio
    async def test_switching_provider_takes_effect_without_rebuild(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Flipping ai.provider on the same settings cache changes which
        provider the next analyze() call reaches, with no new analyst
        constructed. Regression test for the constructor-captured-model bug.
        """
        import mail_verdict.core.anthropic_provider as anthropic_mod
        import mail_verdict.core.openai_provider as openai_mod

        anthropic_client = MagicMock()
        anthropic_client.messages = MagicMock()
        anthropic_client.messages.create = AsyncMock(
            return_value=_anthropic_response('{"verdict": "not-spam", "reasoning": "Fine."}')
        )
        openai_client = MagicMock()
        openai_client.responses = MagicMock()
        openai_client.responses.create = AsyncMock(
            return_value=_openai_response('{"verdict": "spam", "reasoning": "Scam."}')
        )
        monkeypatch.setattr(anthropic_mod, "get_anthropic_client", lambda api_key: anthropic_client)
        monkeypatch.setattr(openai_mod, "get_openai_client", lambda api_key: openai_client)

        settings = _SettingsStub(ai={"provider": "anthropic", "model": "claude-haiku-4-5"})
        analyst = LiveSpamAnalyst(settings, _CredRepoStub())

        first = await analyst.analyze(_make_context())
        assert first.is_spam is False
        anthropic_client.messages.create.assert_awaited_once()
        openai_client.responses.create.assert_not_awaited()

        settings.set_provider("openai")

        second = await analyst.analyze(_make_context())
        assert second.is_spam is True
        openai_client.responses.create.assert_awaited_once()
        # Anthropic was not called again -- the switch was clean, not additive.
        anthropic_client.messages.create.assert_awaited_once()


class TestFakeSpamAnalyst:
    """Tests for FakeSpamAnalyst -- the deterministic test workhorse."""

    @pytest.mark.asyncio
    async def test_flags_keyword_in_subject(self) -> None:
        analyst = FakeSpamAnalyst()
        ctx = AnalysisContext(
            mail_id="m-1", from_addr=None, to_addrs=None,
            subject="Cheap viagra now", body_excerpt="",
        )
        verdict = await analyst.analyze(ctx)
        assert verdict.is_spam is True
        assert "viagra" in verdict.reasoning

    @pytest.mark.asyncio
    async def test_no_keyword_is_not_spam(self) -> None:
        analyst = FakeSpamAnalyst()
        ctx = AnalysisContext(
            mail_id="m-1", from_addr=None, to_addrs=None,
            subject="Team meeting notes", body_excerpt="See you at 3pm.",
        )
        verdict = await analyst.analyze(ctx)
        assert verdict.is_spam is False

    @pytest.mark.asyncio
    async def test_live_analyst_dispatches_to_fake_provider(self) -> None:
        """ai.provider="fake" never reaches a real client."""
        analyst = LiveSpamAnalyst(_SettingsStub(ai={"provider": "fake"}), _CredRepoStub(keys={}))
        ctx = AnalysisContext(
            mail_id="m-1", from_addr=None, to_addrs=None,
            subject="wire transfer needed", body_excerpt="",
        )
        verdict = await analyst.analyze(ctx)
        assert verdict.is_spam is True
