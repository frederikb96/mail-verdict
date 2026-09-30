"""
The orders stage's four outcomes, with a fake OrderLookup: disabled
account, spam verdict, bypass on a known thread, and an ordinary pattern
match/non-match.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from mail_verdict.database.models import VerdictSource
from mail_verdict.pipeline.context import BoundLog, MessageHistory, RunContext, VerdictView
from mail_verdict.pipeline.contracts import EnqueueOrder
from mail_verdict.pipeline.message_view import FolderView, MessageView
from mail_verdict.pipeline.stages.orders import OrdersConfig, OrdersStage

_DEFAULT_FILTER = {
    "include": {"subject": ["bestell", "order"]},
}


class _FakeOrderLookup:
    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.calls: list[tuple[uuid.UUID, uuid.UUID | None, str]] = []

    async def known(
        self, *, account_id: uuid.UUID, thread_id: uuid.UUID | None, text: str,
    ) -> str | None:
        self.calls.append((account_id, thread_id, text))
        return self.answer


def _view(
    *, subject: str = "hello", from_addr: str = "shop@example.com",
    body_text_raw: str | None = "just checking in", thread_id: uuid.UUID | None = None,
) -> MessageView:
    return MessageView(
        message_id=uuid.uuid4(), msg_key="<orders-test@example.com>", account_id=uuid.uuid4(),
        folder=FolderView(id=uuid.uuid4(), imap_name="INBOX", special_use=None),
        subject=subject, from_addr=from_addr, to_addrs=("me@example.com",), cc_addrs=(),
        headers={}, body="", body_truncated=False, size_bytes=0,
        received_at=datetime(2026, 6, 1, tzinfo=timezone.utc), is_seen=False, is_flagged=False,
        is_draft=False, is_truncated=False, keywords=(), tags=(), attachment_types=(),
        has_attachments=False, thread_id=thread_id,
        body_text_raw=body_text_raw, body_html_raw=None,
    )


def _ctx(
    *, orders_enabled: bool, is_spam: bool | None, lookup_answer: str | None,
) -> RunContext:
    verdict = (
        VerdictView(is_spam=is_spam, source=VerdictSource.AI, reasoning=None, created_at=None)
        if is_spam is not None else None
    )
    return RunContext(
        run_id=uuid.uuid4(), account_id=uuid.uuid4(), origin="live", apply=True,
        settings={"orders": {"filter": _DEFAULT_FILTER}}, trace=(), facts={},
        verdict=verdict, history=MessageHistory(has_ai_verdict=False),
        folders=None, neighbors=None, models=None,  # type: ignore[arg-type]
        log=BoundLog(logging.getLogger("test")),
        account_spam_enabled=False, account_orders_enabled=orders_enabled,
        orders=_FakeOrderLookup(lookup_answer),  # type: ignore[arg-type]
    )


def _run(stage: OrdersStage, view: MessageView, ctx: RunContext):
    return asyncio.run(stage.execute(view, ctx))


class TestOrdersStage:
    def setup_method(self) -> None:
        self.stage = OrdersStage("orders", OrdersConfig())

    def test_disabled_account_never_matches(self) -> None:
        outcome = _run(
            self.stage, _view(subject="your order"),
            _ctx(orders_enabled=False, is_spam=None, lookup_answer=None),
        )
        assert outcome.matched is False
        assert "disabled" in (outcome.detail or "")

    def test_a_spam_verdict_never_matches(self) -> None:
        outcome = _run(
            self.stage, _view(subject="your order"),
            _ctx(orders_enabled=True, is_spam=True, lookup_answer=None),
        )
        assert outcome.matched is False
        assert outcome.detail == "spam"

    def test_a_known_thread_bypasses_the_pattern_filter(self) -> None:
        outcome = _run(
            self.stage, _view(subject="Re: question", thread_id=uuid.uuid4()),
            _ctx(orders_enabled=True, is_spam=False, lookup_answer="thread"),
        )
        assert outcome.matched is True
        assert outcome.effects == (EnqueueOrder(reason="thread"),)

    def test_a_matching_subject_pattern_enqueues(self) -> None:
        outcome = _run(
            self.stage, _view(subject="Your Bestellung is on the way"),
            _ctx(orders_enabled=True, is_spam=False, lookup_answer=None),
        )
        assert outcome.matched is True
        assert outcome.effects == (EnqueueOrder(reason="subject:bestell"),)

    def test_no_pattern_match_and_no_bypass_never_matches(self) -> None:
        outcome = _run(
            self.stage, _view(subject="Weekly digest"),
            _ctx(orders_enabled=True, is_spam=False, lookup_answer=None),
        )
        assert outcome.matched is False
        assert outcome.detail == "no pattern matched"
