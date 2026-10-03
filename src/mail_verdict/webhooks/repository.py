"""
The webhook queue's table operations: enqueueing a delivery (the one place
the insert is written), listing, and re-queueing a failed one.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mail_verdict.database.models import WebhookDelivery
from mail_verdict.queue.notify import WorkQueueNotifier

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from mail_verdict.pipeline.contracts import Webhook

QUEUE_NAME = "webhooks"


def delivery_config(effect: Webhook) -> dict[str, Any]:
    """The destination snapshot stored on a delivery row."""
    return {
        "url": effect.url, "method": effect.method, "headers": dict(effect.headers),
        "received_at_param": effect.received_at_param,
    }


async def enqueue_delivery(
    session: AsyncSession, effect: Webhook, *, account_id: uuid.UUID, msg_key: str,
    message_id: uuid.UUID | None, origin: str, priority: int,
    next_attempt_at: datetime | None = None,
) -> bool:
    """
    Insert one pending delivery unless this mail already has a row for this
    webhook name, whatever its status -- the never-twice gate.

    Args:
        session: Session to write on; the caller commits
        effect: The rule's webhook action
        account_id: The mail's account
        msg_key: The mail's durable identity (database/msg_key.py)
        message_id: The mail's current row id, a hint only
        origin: 'live' for a rule pass, 'backfill' for the supported backfill
        priority: Lower is claimed first; backfill sits behind live mail
        next_attempt_at: When the row becomes due; now when omitted

    Returns:
        True only when a new row was inserted
    """
    values: dict[str, Any] = {
        "name": effect.name, "account_id": account_id, "msg_key": msg_key,
        "message_id": message_id, "origin": origin, "priority": priority,
        "config": delivery_config(effect),
    }
    if next_attempt_at is not None:
        values["next_attempt_at"] = next_attempt_at
    result = await session.execute(
        pg_insert(WebhookDelivery).values(**values).on_conflict_do_nothing(
            index_elements=["name", "account_id", "msg_key"],
        )
    )
    inserted = bool(result.rowcount)  # type: ignore[attr-defined]
    if inserted:
        await WorkQueueNotifier.notify(session, QUEUE_NAME)
    return inserted


async def list_deliveries(
    session: AsyncSession, *, name: str | None, status: str | None, limit: int,
) -> list[WebhookDelivery]:
    """Newest first, optionally narrowed by webhook name and status."""
    stmt = select(WebhookDelivery).order_by(WebhookDelivery.created_at.desc()).limit(limit)
    if name is not None:
        stmt = stmt.where(WebhookDelivery.name == name)
    if status is not None:
        stmt = stmt.where(WebhookDelivery.status == status)
    return list((await session.execute(stmt)).scalars().all())


async def counts_by_status(session: AsyncSession, *, name: str) -> dict[str, int]:
    """Delivery rows per status for one webhook name."""
    rows = (
        await session.execute(
            select(WebhookDelivery.status, func.count())
            .where(WebhookDelivery.name == name).group_by(WebhookDelivery.status)
        )
    ).all()
    return {status: count for status, count in rows}


async def requeue_failed(session: AsyncSession, delivery_id: uuid.UUID) -> bool:
    """
    Put a failed delivery back to pending with a fresh attempt budget.

    Only a `failed` row moves: a delivered row is never re-sent, and one
    still pending or claimed is already on its way. Bumps `generation` so
    the retry's own failure alerts again.

    Returns:
        Whether a failed row was found and re-queued
    """
    result = await session.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == delivery_id, WebhookDelivery.status == "failed")
        .values(
            status="pending", attempts=0, generation=WebhookDelivery.generation + 1,
            next_attempt_at=datetime.now(timezone.utc), last_error=None, http_status=None,
        )
    )
    requeued = bool(result.rowcount)  # type: ignore[attr-defined]
    if requeued:
        await WorkQueueNotifier.notify(session, QUEUE_NAME)
    return requeued
