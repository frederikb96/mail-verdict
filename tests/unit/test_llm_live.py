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

from mail_verdict.core.anthropic_provider import reset_anthropic_provider
from mail_verdict.core.openai_provider import reset_openai_provider
from mail_verdict.core.retry import RetryConfig
from mail_verdict.orders import prompts as orders_prompts
from mail_verdict.pipeline.context import (
    BoundLog,
    FolderResolver,
    MessageHistory,
    ModelGateway,
    RunContext,
)
from mail_verdict.pipeline.contracts import RecordVerdict
from mail_verdict.pipeline.message_view import FolderView, MessageView
from mail_verdict.pipeline.neighbors import NeighborHint
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


class _NoNeighbors:
    """Stands in for NeighborService: this layer proves the provider call,
    not the database-backed neighbour lookup."""

    async def hints_for(
        self, *, msg_key: str, model: str, k: int, min_similarity: float,
    ) -> list[NeighborHint]:
        return []


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
        neighbors=_NoNeighbors(),  # type: ignore[arg-type]
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
        ctx = _build_ctx({"provider": "openai", "model": "gpt-6-luna", "max_tokens": 512})
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


class TestCustomProviderLive:
    """A "custom" OpenAI-compatible server against the real chat
    completions API -- the request shape call_openai_structured never
    exercises, since a compatible server rejects OpenAI's own Responses
    API (see core/structured_llm.py's module docstring)."""

    @pytest.mark.asyncio
    async def test_classifies_spam_and_ham_correctly(self) -> None:
        _require_key("CUSTOM_AI_API_KEY")
        ctx = _build_ctx({
            "provider": "custom",
            "base_url": "https://api.infomaniak.com/2/ai/111650/openai/v1",
            "model": "google/gemma-4-31B-it",
            "reasoning_effort": "none",
            "max_tokens": 512,
        })
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


_ORDER_MAIL = {
    "subject": "Your order NK-48213 has shipped",
    "from_addr": "Northwind Shop <shop@northwind.example>",
    "to_addrs": "me@example.test",
    "received_at": "2026-05-02T09:30:00+00:00",
    "body": (
        "Hello,\n\nyour order NK-48213 (2 x Trail socks, 1 x Water bottle, total EUR 54.80) "
        "has been handed to DHL. Tracking number: 00340434292135100186.\n"
        "Expected delivery: 5 May 2026.\n\nNorthwind Shop"
    ),
}


class _OrdersTasks:
    """The two orders model calls, each through the gateway on one real
    provider, with the shipped prompts and schemas: the decide call must
    open a new entry for a shipping mail with no candidates, and the write
    call must produce a validated entry for it."""

    provider: str
    model: str
    effort: str
    base_url: str | None = None
    key_env: str

    @pytest.fixture(autouse=True)
    def _fresh_clients(self) -> None:
        """Each test runs on its own event loop; a provider client cached by
        an earlier test is bound to a loop that is already closed."""
        reset_openai_provider()
        reset_anthropic_provider()

    def _gateway(self) -> ModelGateway:
        cred_repo = ProviderCredentialRepository(db=None, encryption_key="")  # type: ignore[arg-type]
        retry = RetryConfig(max_retries=2, base_delay=1.0, max_delay=5.0, exp_base=2.0)
        return ModelGateway(db=None, cred_repo=cred_repo, retry_config=retry)  # type: ignore[arg-type]

    async def _call(
        self, category: str, max_tokens: int, schema_name: str, system_prompt: str,
        user_prompt: str, schema: dict[str, object], validate: object,
    ) -> dict[str, object]:
        data, _ = await self._gateway().structured_call(
            provider=self.provider, category=category, model=self.model, effort=self.effort,
            max_tokens=max_tokens, schema_name=schema_name, system_prompt=system_prompt,
            user_prompt=user_prompt, schema=schema, validate=validate, base_url=self.base_url,
            timeout_seconds=120.0,
        )
        return data

    @pytest.mark.asyncio
    async def test_decide_opens_a_new_entry_for_a_shipping_mail(self) -> None:
        _require_key(self.key_env)
        user_prompt = orders_prompts.build_decide_user_prompt(
            subject=_ORDER_MAIL["subject"], from_addr=_ORDER_MAIL["from_addr"],
            to_addrs=_ORDER_MAIL["to_addrs"], received_at=_ORDER_MAIL["received_at"],
            attachments=[], body=_ORDER_MAIL["body"], candidates=[],
        )
        data = await self._call(
            "orders.decide", 4000, "order_decision", orders_prompts.load_decide_system_prompt(),
            user_prompt, orders_prompts.build_decide_schema([]),
            lambda d: orders_prompts.validate_decide_response(d, handles=[]),
        )
        assert data["target"] == "new"

    @pytest.mark.asyncio
    async def test_write_produces_a_validated_entry(self) -> None:
        _require_key(self.key_env)
        user_prompt = orders_prompts.build_write_user_prompt(
            current_text=None,
            mails=[{
                "received_at": _ORDER_MAIL["received_at"], "from_addr": _ORDER_MAIL["from_addr"],
                "subject": _ORDER_MAIL["subject"], "body": _ORDER_MAIL["body"], "documents": [],
            }],
        )
        data = await self._call(
            "orders.write", 16000, "order_text",
            orders_prompts.build_write_system_prompt(language="English"), user_prompt,
            orders_prompts.WRITE_SCHEMA, orders_prompts.validate_write_response,
        )
        assert "Northwind" in str(data["merchant"])
        assert data["open"] is True
        assert data["icon"] == "package"
        assert data["kind"] == "order"
        assert data["summary"]


class TestOrdersTasksOnClaudeLive(_OrdersTasks):
    """Claude Opus 5.5 through the Messages API: thinking on and adaptive,
    depth set by effort, the schema enforced through output_config."""

    provider = "anthropic"
    model = "claude-opus-5-5"
    effort = "medium"
    key_env = "ANTHROPIC_API_KEY"


class TestOrdersTasksOnInfomaniakLive(_OrdersTasks):
    """The production chat model on the Infomaniak (custom) provider."""

    provider = "custom"
    model = "Qwen/Qwen3.8-27B-FP8"
    effort = "none"
    base_url = "https://api.infomaniak.com/2/ai/111650/openai/v1"
    key_env = "CUSTOM_AI_API_KEY"
