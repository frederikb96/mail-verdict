"""
Classification against the real language model, both providers.

Marked `llm`, excluded from CI (`pytest tests/unit/ -m "not llm"`) -- run
deliberately with ANTHROPIC_API_KEY / OPENAI_API_KEY set. Asserts the key
is present and fails loudly rather than skipping, per this project's rule
that a skipped test is indistinguishable from a passing one.

A handful of real calls, not a suite: one spam and one ham fixture per
provider, enough to prove the strict-schema request shape and the retry
path actually work against each API, not just against a mock.
"""

from __future__ import annotations

import email
import email.policy
import os
from email.message import EmailMessage
from pathlib import Path

import pytest

from mail_verdict.settings.credentials import ProviderCredentialRepository
from mail_verdict.spam.analyst import AnalysisContext, LiveSpamAnalyst

pytestmark = pytest.mark.llm

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "emails"


class _SettingsStub:
    """Minimal SettingsService stand-in with fixed ai/retry settings."""

    def __init__(self, ai: dict[str, object]) -> None:
        self._ai = ai
        self._retry = {
            "max_retries": 3, "base_delay_seconds": 1.0,
            "max_delay_seconds": 20.0, "exponential_base": 2.0,
        }

    def get(self, category: str) -> dict[str, object]:
        return {"ai": self._ai, "retry": self._retry}.get(category, {})


def _load_context(filename: str) -> AnalysisContext:
    """Build an AnalysisContext from a fixture .eml file."""
    raw = (FIXTURES_DIR / filename).read_bytes()
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    body = msg.get_body(preferencelist=("plain",))
    body_text = body.get_content() if body is not None else ""
    return AnalysisContext(
        mail_id=filename,
        from_addr=msg.get("From"),
        to_addrs=msg.get("To"),
        subject=msg.get("Subject"),
        body_excerpt=body_text[:1000],
    )


def _require_key(env_var: str) -> None:
    """Fail loudly, not skip, when the key this layer needs is absent."""
    if not os.environ.get(env_var):
        pytest.fail(f"{env_var} is required for tests/unit/test_llm_live.py -- not skipped")


class TestOpenAILive:
    """The OpenAI path against the real Responses API."""

    @pytest.mark.asyncio
    async def test_classifies_spam_and_ham_correctly(self) -> None:
        _require_key("OPENAI_API_KEY")
        cred_repo = ProviderCredentialRepository(db=None, encryption_key="")  # type: ignore[arg-type]
        analyst = LiveSpamAnalyst(
            _SettingsStub({"provider": "openai", "model": "gpt-5.4-nano", "max_tokens": 512}),
            cred_repo,
        )

        spam_verdict = await analyst.analyze(_load_context("spam_pharmacy.eml"))
        ham_verdict = await analyst.analyze(_load_context("ham_simple.eml"))

        assert spam_verdict.is_spam is True
        assert ham_verdict.is_spam is False
        assert spam_verdict.reasoning and ham_verdict.reasoning


class TestAnthropicLive:
    """The Anthropic path against the real Messages API."""

    @pytest.mark.asyncio
    async def test_classifies_spam_and_ham_correctly(self) -> None:
        _require_key("ANTHROPIC_API_KEY")
        cred_repo = ProviderCredentialRepository(db=None, encryption_key="")  # type: ignore[arg-type]
        ai_settings = {"provider": "anthropic", "model": "claude-haiku-4-5", "max_tokens": 512}
        analyst = LiveSpamAnalyst(_SettingsStub(ai_settings), cred_repo)

        spam_verdict = await analyst.analyze(_load_context("spam_pharmacy.eml"))
        ham_verdict = await analyst.analyze(_load_context("ham_simple.eml"))

        assert spam_verdict.is_spam is True
        assert ham_verdict.is_spam is False
        assert spam_verdict.reasoning and ham_verdict.reasoning
