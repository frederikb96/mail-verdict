"""
Writes over the orders register: attach, recompute, store identifiers,
write text, detach, move, merge, delete.

Every function takes an already-open AsyncSession rather than opening its
own -- the worker (orders/worker.py) runs a whole mail job inside one
transaction, held by the advisory lock that makes "never split" true by
construction, and these functions are what runs inside it. The API
endpoints (api/orders.py) that call the same functions open their own
session per request, the ordinary pattern everywhere else in this
codebase.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import Order, OrderIdentifier, OrderJob, OrderMail
from mail_verdict.orders.candidates import is_valid_identifier, normalize_identifier
from mail_verdict.orders.text import cut_status, cut_subject, cut_summary, summary_preview

_IDENTIFIER_WINDOW_DAYS = 365


async def create_order(session: AsyncSession) -> uuid.UUID:
    """Insert a new, unwritten order -- text_stale true, written_at null."""
    result = await session.execute(
        insert(Order).values(text_stale=True).returning(Order.id)
    )
    return uuid.UUID(str(result.scalar_one()))


async def attach_mail(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    account_id: uuid.UUID,
    msg_key: str,
    message_id: uuid.UUID | None,
    thread_id: uuid.UUID | None,
    subject: str | None,
    from_addr: str | None,
    received_at: datetime,
    attached_by: str,
) -> bool:
    """
    Attach a mail to an order.

    Returns:
        True if a new order_mails row was inserted; False if this
        (account_id, msg_key) was already attached to some order (the
        never-twice gate -- see uq_order_mails_account_msg_key).
    """
    stmt = (
        pg_insert(OrderMail)
        .values(
            order_id=order_id, account_id=account_id, msg_key=msg_key, message_id=message_id,
            thread_id=thread_id, subject=subject, from_addr=from_addr, received_at=received_at,
            attached_by=attached_by,
        )
        .on_conflict_do_nothing(constraint="uq_order_mails_account_msg_key")
    )
    result = await session.execute(stmt)
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def recompute_aggregates(session: AsyncSession, order_id: uuid.UUID) -> int:
    """
    Recompute mail_count/first_mail_at/last_mail_at from order_mails.

    Returns:
        The recomputed mail_count -- callers use 0 to know an order is
        now empty and should be deleted.
    """
    agg_result = await session.execute(
        select(
            func.count(OrderMail.id), func.min(OrderMail.received_at),
            func.max(OrderMail.received_at),
        ).where(OrderMail.order_id == order_id)
    )
    count, first_at, last_at = agg_result.one()
    await session.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(
            mail_count=count, first_mail_at=first_at, last_mail_at=last_at, updated_at=func.now(),
        )
    )
    return int(count)


async def store_identifiers(
    session: AsyncSession, order_id: uuid.UUID, identifiers: list[tuple[str, str]],
) -> None:
    """
    Store every valid identifier the model reported, skipping one already
    held by ANOTHER order active in the last 365 days -- a duplicate
    number is evidence the decide call chose the wrong candidate, not a
    reason to make two orders claim the same number.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=_IDENTIFIER_WINDOW_DAYS)
    for kind, value in identifiers:
        if not is_valid_identifier(value):
            continue
        value_norm = normalize_identifier(value.strip())
        held_elsewhere = await session.execute(
            select(OrderIdentifier.id)
            .join(Order, Order.id == OrderIdentifier.order_id)
            .where(
                OrderIdentifier.value_norm == value_norm, OrderIdentifier.order_id != order_id,
                Order.last_mail_at.is_not(None), Order.last_mail_at >= cutoff,
            )
            .limit(1)
        )
        if held_elsewhere.scalar_one_or_none() is not None:
            continue
        stmt = (
            pg_insert(OrderIdentifier)
            .values(order_id=order_id, kind=kind, value=value.strip(), value_norm=value_norm)
            .on_conflict_do_nothing(constraint="uq_order_identifiers_order_value_norm")
        )
        await session.execute(stmt)


async def enqueue_mail_job(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    msg_key: str,
    message_id: uuid.UUID | None,
    origin: str,
    priority: int,
    filter_reason: str,
    next_attempt_at: datetime,
) -> bool:
    """
    Insert the orders queue's mail job, under the never-twice gate
    (uq_order_jobs_mail): a duplicate is a no-op on the job row itself,
    but still refreshes message_id when a resync gave this mail a new
    row -- so a later worker claim resolves the current mail, not a stale
    id from before a UIDVALIDITY change.

    Shared by the pipeline stage's EnqueueOrder effect
    (pipeline/effects.py) and the thread follow-up hook (orders/
    intake.py) -- the one place this insert is written.

    Returns:
        True only when a new row was inserted.
    """
    stmt = (
        pg_insert(OrderJob)
        .values(
            kind="mail", account_id=account_id, msg_key=msg_key, message_id=message_id,
            origin=origin, priority=priority, next_attempt_at=next_attempt_at,
            filter_reason=filter_reason,
        )
        .on_conflict_do_nothing(
            index_elements=["account_id", "msg_key"], index_where=text("kind = 'mail'"),
        )
    )
    result = await session.execute(stmt)
    inserted = bool(result.rowcount)  # type: ignore[attr-defined]
    if inserted:
        return True

    await session.execute(
        update(OrderJob)
        .where(
            OrderJob.kind == "mail", OrderJob.account_id == account_id,
            OrderJob.msg_key == msg_key, OrderJob.message_id.is_distinct_from(message_id),
        )
        .values(message_id=message_id)
    )
    return False


async def enqueue_write_job(
    session: AsyncSession, order_id: uuid.UUID, *, priority: int, origin: str = "live",
) -> None:
    """Enqueue a write job for an order, a no-op if a pending one already
    exists (uq_order_jobs_write)."""
    stmt = (
        pg_insert(OrderJob)
        .values(kind="write", order_id=order_id, origin=origin, priority=priority)
        .on_conflict_do_nothing(constraint="uq_order_jobs_write")
    )
    await session.execute(stmt)


@dataclass(frozen=True)
class WriteResult:
    order_id: uuid.UUID
    created: bool  # True the first time an order is ever written
    summary_preview: str


async def write_order_text(
    session: AsyncSession,
    order_id: uuid.UUID,
    *,
    merchant: str,
    subject: str,
    status: str,
    is_open: bool,
    icon: str,
    summary: str,
    model: str | None,
) -> WriteResult | None:
    """
    Store a write call's answer.

    Returns:
        None if the order no longer exists (deleted mid-flight); the
        result, `created=True` when this is the order's first write,
        otherwise
    """
    row = await session.execute(select(Order.written_at).where(Order.id == order_id))
    existing = row.one_or_none()
    if existing is None:
        return None
    created = existing.written_at is None

    preview = summary_preview(summary)
    await session.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(
            merchant=merchant.strip()[:120], subject=cut_subject(subject),
            status=cut_status(status), is_open=is_open, icon=icon,
            summary=cut_summary(summary), summary_preview=preview,
            text_stale=False, model=model, updated_at=func.now(),
            written_at=func.coalesce(Order.written_at, func.now()),
        )
    )
    return WriteResult(order_id=order_id, created=created, summary_preview=preview)


async def mark_text_stale(session: AsyncSession, order_id: uuid.UUID) -> None:
    await session.execute(
        update(Order).where(Order.id == order_id).values(text_stale=True, updated_at=func.now())
    )


async def delete_order_if_empty(session: AsyncSession, order_id: uuid.UUID) -> bool:
    """Delete an order left with no mail. Returns whether it was deleted."""
    result = await session.execute(
        delete(Order).where(Order.id == order_id, Order.mail_count == 0)
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def detach_mail(
    session: AsyncSession, order_mail_id: uuid.UUID,
) -> uuid.UUID | None:
    """
    Remove one mail from its order.

    Returns:
        The order_id it was removed from, or None if the row was already
        gone
    """
    result = await session.execute(
        delete(OrderMail).where(OrderMail.id == order_mail_id).returning(OrderMail.order_id)
    )
    row = result.one_or_none()
    return row[0] if row is not None else None


async def merge_order(session: AsyncSession, *, source_id: uuid.UUID, target_id: uuid.UUID) -> None:
    """
    Merge `source_id` into `target_id`: every mail and every identifier
    moves to the target, and the source is deleted. The repair for a
    split the model made in error.
    """
    await session.execute(
        update(OrderMail).where(OrderMail.order_id == source_id).values(order_id=target_id)
    )
    source_identifiers = await session.execute(
        select(OrderIdentifier.kind, OrderIdentifier.value, OrderIdentifier.value_norm).where(
            OrderIdentifier.order_id == source_id,
        )
    )
    for kind, value, value_norm in source_identifiers.all():
        stmt = (
            pg_insert(OrderIdentifier)
            .values(order_id=target_id, kind=kind, value=value, value_norm=value_norm)
            .on_conflict_do_nothing(constraint="uq_order_identifiers_order_value_norm")
        )
        await session.execute(stmt)
    await session.execute(delete(Order).where(Order.id == source_id))
    await recompute_aggregates(session, target_id)


async def delete_order(session: AsyncSession, order_id: uuid.UUID) -> bool:
    """Delete an order and its membership/numbers. No mail is touched."""
    result = await session.execute(delete(Order).where(Order.id == order_id))
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def delete_for_account(
    session: AsyncSession, account_id: uuid.UUID,
) -> list[tuple[uuid.UUID, str]]:
    """
    Account deletion's cleanup: remove this account's order_mails and
    order_jobs, delete orders left with no mail, and enqueue a write for
    orders that lost some but still have mail from another account.

    Returns:
        (order_id, change) pairs -- "deleted" or "updated" -- for the
        caller to broadcast order.updated with, once its own transaction
        has committed. Orders is a cross-account register with no
        account-scoped event of its own to ride: a second browser with
        one of these orders open only learns of the change this way.
    """
    affected = await session.execute(
        select(OrderMail.order_id.distinct()).where(OrderMail.account_id == account_id)
    )
    order_ids = [row[0] for row in affected.all()]

    await session.execute(delete(OrderMail).where(OrderMail.account_id == account_id))
    await session.execute(delete(OrderJob).where(OrderJob.account_id == account_id))

    changes: list[tuple[uuid.UUID, str]] = []
    for order_id in order_ids:
        remaining = await recompute_aggregates(session, order_id)
        if remaining == 0:
            await delete_order_if_empty(session, order_id)
            changes.append((order_id, "deleted"))
        else:
            await mark_text_stale(session, order_id)
            await enqueue_write_job(session, order_id, priority=50)
            changes.append((order_id, "updated"))
    return changes
