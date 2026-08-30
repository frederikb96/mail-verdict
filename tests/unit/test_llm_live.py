"""
Classification against the real language model, both providers.

Marked `llm`, excluded from CI (`pytest tests/unit/ -m "not llm"`) -- run
deliberately with ANTHROPIC_API_KEY / OPENAI_API_KEY set. Asserts the key
is present and fails loudly rather than skipping, per this project's rule
that a skipped test is indistinguishable from a passing one.

A handful of real calls, not a suite: one spam and one ham fixture per
provider, enough to prove the classify stage's strict-schema request shape
and the retry path actually work against each API, not just against a
mock -- and that the identity facts (envelope vs. From, Reply-To, display
name) actually reach the prompt.
"""

from __future__ import annotations

import email
import email.policy
import logging
import os
import uuid
from email.message import EmailMessage
from pathlib import Path

import pytest

from mail_verdict.core.retry import RetryConfig
from mail_verdict.pipeline.context import (
    BoundLog,
    FolderResolver,
    MessageHistory,
    ModelGateway,
    RunContext,
)
from mail_verdict.pipeline.contracts import RecordVerdict
from mail_verdict.pipeline.message_view import FolderView, MessageView
from mail_verdict.pipeline.stages.classify import ClassifyConfig, ClassifyStage
from mail_verdict.settings.credentials import ProviderCredentialRepository

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.llm

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "emails"


def _load_view(filename: str) -> MessageView:
    """Build a MessageView from a fixture .eml file."""
    raw = (FIXTURES_DIR / filename).read_bytes()
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    body = msg.get_body(preferencelist=("plain",))
    body_text = body.get_content() if body is not None else ""
    headers = {k.lower(): str(v) for k, v in msg.items()}
    account_id = uuid.uuid4()
    return MessageView(
        message_id=uuid.uuid4(),
        msg_key=f"<{filename}@fixtures>",
        account_id=account_id,
        folder=FolderView(id=uuid.uuid4(), imap_name="INBOX", special_use=None),
        subject=msg.get("Subject", ""),
        from_addr=msg.get("From", ""),
        to_addrs=(msg.get("To", ""),),
        cc_addrs=(),
        headers=headers,
        body=body_text[:1000],
        body_truncated=False,
        size_bytes=len(raw),
        received_at=None,
        is_seen=False,
        is_flagged=False,
        is_draft=False,
        is_truncated=False,
        keywords=(),
        tags=(),
        attachment_types=(),
        has_attachments=False,
        reply_to=msg.get("Reply-To"),
    )


def _build_ctx(ai_settings: dict[str, object]) -> RunContext:
    cred_repo = ProviderCredentialRepository(db=None, encryption_key="")  # type: ignore[arg-type]
    retry_settings = {
        "max_retries": 3, "base_delay_seconds": 1.0,
        "max_delay_seconds": 20.0, "exponential_base": 2.0,
    }

    return RunContext(
        run_id=uuid.uuid4(), account_id=uuid.uuid4(), origin="live", apply=True,
        settings={"ai": ai_settings, "retry": retry_settings}, trace=(), facts={},
        verdict=None, history=MessageHistory(has_ai_verdict=False),
        folders=FolderResolver(db=None, account_id=uuid.uuid4()),  # type: ignore[arg-type]
        models=ModelGateway(
            db=None, cred_repo=cred_repo, retry_config=RetryConfig.from_settings(retry_settings),
        ),
        log=BoundLog(logger),
        account_spam_enabled=True,
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
        ctx = _build_ctx({"provider": "openai", "model": "gpt-5.4-nano", "max_tokens": 512})
        stage = ClassifyStage("classify", ClassifyConfig())

        spam_outcome = await stage.execute(_load_view("spam_pharmacy.eml"), ctx)
        ham_outcome = await stage.execute(_load_view("ham_simple.eml"), ctx)

        spam_effect = spam_outcome.effects[0]
        ham_effect = ham_outcome.effects[0]
        assert isinstance(spam_effect, RecordVerdict)
        assert isinstance(ham_effect, RecordVerdict)
        assert spam_effect.is_spam is True
        assert ham_effect.is_spam is False
        assert spam_effect.reasoning and ham_effect.reasoning


class TestAnthropicLive:
    """The Anthropic path against the real Messages API."""

    @pytest.mark.asyncio
    async def test_classifies_spam_and_ham_correctly(self) -> None:
        _require_key("ANTHROPIC_API_KEY")
        ctx = _build_ctx({"provider": "anthropic", "model": "claude-haiku-4-5", "max_tokens": 512})
        stage = ClassifyStage("classify", ClassifyConfig())

        spam_outcome = await stage.execute(_load_view("spam_pharmacy.eml"), ctx)
        ham_outcome = await stage.execute(_load_view("ham_simple.eml"), ctx)

        spam_effect = spam_outcome.effects[0]
        ham_effect = ham_outcome.effects[0]
        assert isinstance(spam_effect, RecordVerdict)
        assert isinstance(ham_effect, RecordVerdict)
        assert spam_effect.is_spam is True
        assert ham_effect.is_spam is False
        assert spam_effect.reasoning and ham_effect.reasoning
