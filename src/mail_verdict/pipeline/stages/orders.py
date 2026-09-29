"""
The `orders` stage: checks the per-account switch, the spam verdict and
the first filter, and only ever ENQUEUES a job -- it never calls a model
itself.

Deliberate, for two reasons: a burst of mail from one sender (observed
during design, three mails in the same minute) runs through the pipeline
concurrently, and two workers deciding at once could each see no matching
order and each open one -- a single-worker queue with an advisory lock
(orders/worker.py) is what makes "never split" hold by construction
rather than by luck. And a model call inside this stage would delay every
new-mail alert behind it (alerts/dispatch.py waits for the pipeline run
to finish), for a decision the alert does not need.
"""

from __future__ import annotations

import builtins
from typing import ClassVar

from pydantic import BaseModel

from mail_verdict.orders.content import prepare_body
from mail_verdict.orders.filter import evaluate_filter
from mail_verdict.pipeline.context import RunContext
from mail_verdict.pipeline.contracts import EnqueueOrder, StageOutcome
from mail_verdict.pipeline.message_view import MessageView


class OrdersConfig(BaseModel):
    """Empty -- the first filter's patterns live in settings.orders.filter,
    read fresh per run, not in stage config (see orders/filter.py)."""


class OrdersStage:
    """Filters and enqueues; never calls a model."""

    type: ClassVar[str] = "orders"
    runs_on: ClassVar[frozenset[str]] = frozenset({"live"})

    @classmethod
    def config_schema(cls) -> builtins.type[BaseModel]:
        return OrdersConfig

    def __init__(self, stage_id: str, config: OrdersConfig) -> None:
        self._stage_id = stage_id
        self._config = config

    async def execute(self, msg: MessageView, ctx: RunContext) -> StageOutcome:
        if not ctx.account_orders_enabled:
            return StageOutcome(matched=False, detail="orders disabled for this account")
        if ctx.verdict is not None and ctx.verdict.is_spam:
            return StageOutcome(matched=False, detail="spam")

        orders_settings = ctx.settings.get("orders", {})
        prepared = prepare_body(body_text=msg.body_text_raw, body_html=msg.body_html_raw)

        assert ctx.orders is not None  # built by the runner, see pipeline/context.py
        bypass_reason = await ctx.orders.known(
            account_id=ctx.account_id, thread_id=msg.thread_id,
            text=f"{msg.subject}\n{prepared.raw}",
        )
        if bypass_reason is not None:
            return StageOutcome(
                matched=True, effects=(EnqueueOrder(reason=bypass_reason),),
                detail=f"queued: {bypass_reason}",
            )

        result = evaluate_filter(
            subject=msg.subject, from_header=msg.from_addr, body=prepared.text,
            patterns=orders_settings.get("filter", {}),
        )
        if not result.passed:
            return StageOutcome(matched=False, detail="no pattern matched")

        return StageOutcome(
            matched=True, effects=(EnqueueOrder(reason=result.reason or ""),),
            detail=f"queued: {result.reason}",
        )
