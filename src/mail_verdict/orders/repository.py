"""
Writes over the orders register: attach, recompute, store identifier
facts, write text, detach, move, merge, delete.

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
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import (
    Order,
    OrderDocument,
    OrderFact,
    OrderFile,
    OrderJob,
    OrderMail,
)
from mail_verdict.orders.candidates import (
    ACCEPTS_MAIL,
    is_valid_identifier,
    normalize_identifier,
)
from mail_verdict.orders.text import cut_status, cut_subject, cut_summary, summary_preview

_IDENTIFIER_WINDOW_DAYS = 365

# Who decided an order's is_open (orders.open_set_by).
OPEN_SET_BY_MODEL = "ai"
OPEN_SET_BY_USER = "user"
OPEN_SET_BY_AUTO = "auto"

# Who decided an order's kind (orders.kind_set_by).
KIND_SET_BY_MODEL = "ai"
KIND_SET_BY_USER = "user"

# The fact kinds that identify a purchase: candidate retrieval matches mail
# text against them, and they carry value_norm. Every other fact kind is a
# display fact.
IDENTIFIER_FACT_KINDS = (
    "order_number", "booking_code", "ticket_number", "invoice_number", "tracking_number",
)
_IDENTIFIER_LABELS = {
    "order_number": "Order number", "booking_code": "Booking code",
    "ticket_number": "Ticket number", "invoice_number": "Invoice number",
    "tracking_number": "Tracking number",
}


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
    Recompute mail_count/first_mail_at/last_mail_at from order_mails,
    file_count from order_files, and first/last_activity_at over both.

    Returns:
        The recomputed mail_count + file_count -- callers use 0 to know an
        order is now empty and should be deleted.
    """
    mails = (
        select(
            func.count(OrderMail.id).label("n"), func.min(OrderMail.received_at).label("first"),
            func.max(OrderMail.received_at).label("last"),
        )
        .where(OrderMail.order_id == order_id)
        .subquery("mails")
    )
    files = (
        select(
            func.count(OrderFile.id).label("n"), func.min(OrderFile.created_at).label("first"),
            func.max(OrderFile.created_at).label("last"),
        )
        .where(OrderFile.order_id == order_id)
        .subquery("files")
    )
    result = await session.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(
            mail_count=select(mails.c.n).scalar_subquery(),
            first_mail_at=select(mails.c.first).scalar_subquery(),
            last_mail_at=select(mails.c.last).scalar_subquery(),
            file_count=select(files.c.n).scalar_subquery(),
            first_activity_at=select(func.least(mails.c.first, files.c.first)).scalar_subquery(),
            last_activity_at=select(func.greatest(mails.c.last, files.c.last)).scalar_subquery(),
            updated_at=func.now(),
        )
        .returning(Order.mail_count, Order.file_count)
    )
    row = result.one_or_none()
    return int(row.mail_count + row.file_count) if row is not None else 0


async def store_identifier_facts(
    session: AsyncSession, order_id: uuid.UUID, identifiers: list[tuple[str, str]],
    *, source: str = "ai",
) -> None:
    """
    Store every valid identifier the model reported as an identifier-kind
    fact, skipping one already held by ANOTHER order active in the last 365
    days -- a duplicate number is evidence the decide call chose the wrong
    candidate, not a reason to make two orders claim the same number.
    Identifiers only ever accumulate here; display facts are a write call's
    to replace.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=_IDENTIFIER_WINDOW_DAYS)
    for kind, value in identifiers:
        if kind not in IDENTIFIER_FACT_KINDS or not is_valid_identifier(value):
            continue
        value_norm = normalize_identifier(value.strip())
        held_elsewhere = await session.execute(
            select(OrderFact.id)
            .join(Order, Order.id == OrderFact.order_id)
            .where(
                OrderFact.value_norm == value_norm, OrderFact.order_id != order_id,
                Order.last_activity_at.is_not(None), Order.last_activity_at >= cutoff, ACCEPTS_MAIL,
            )
            .limit(1)
        )
        if held_elsewhere.scalar_one_or_none() is not None:
            continue
        next_position = (
            select(func.coalesce(func.max(OrderFact.position) + 1, 0))
            .where(OrderFact.order_id == order_id)
            .scalar_subquery()
        )
        stmt = (
            pg_insert(OrderFact)
            .values(
                order_id=order_id, kind=kind, label=_IDENTIFIER_LABELS[kind],
                value=value.strip(), value_norm=value_norm, position=next_position,
                source=source,
            )
            .on_conflict_do_nothing(
                index_elements=["order_id", "value_norm"],
                index_where=text("value_norm IS NOT NULL"),
            )
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
    exists (uq_order_jobs_write).

    uq_order_jobs_write is a partial unique INDEX, not a table
    CONSTRAINT (it can't be one -- a constraint has no WHERE clause) --
    ON CONFLICT ON CONSTRAINT requires an actual constraint and raises
    UndefinedObjectError against an index of the same name, so this
    infers the arbiter from its columns and predicate instead, the same
    way the uq_order_jobs_mail partial index is targeted elsewhere.
    """
    stmt = (
        pg_insert(OrderJob)
        .values(kind="write", order_id=order_id, origin=origin, priority=priority)
        .on_conflict_do_nothing(
            index_elements=["order_id"],
            index_where=text("kind = 'write' AND status = 'pending'"),
        )
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
    expected_until: date | None = None,
    kind: str | None = None,
    event_start: date | None = None,
    event_end: date | None = None,
) -> WriteResult | None:
    """
    Store a write call's answer.

    is_open only lands while the model owns it (open_set_by = 'ai'): once
    a person or the automatic close has decided, a rewrite leaves the
    decision alone. expected_until is always stored. kind lands the same
    way (kind_set_by = 'ai'); passing kind=None leaves kind and the event
    dates untouched, passing a kind stores event_start/event_end with it
    (None clears them).

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
            status=cut_status(status),
            is_open=case((Order.open_set_by == OPEN_SET_BY_MODEL, is_open), else_=Order.is_open),
            expected_until=expected_until, icon=icon,
            **_kind_values(kind, event_start, event_end),
            summary=cut_summary(summary), summary_preview=preview,
            text_stale=False, model=model, updated_at=func.now(),
            written_at=func.coalesce(Order.written_at, func.now()),
        )
    )
    return WriteResult(order_id=order_id, created=created, summary_preview=preview)


def _kind_values(
    kind: str | None, event_start: date | None, event_end: date | None,
) -> dict[str, Any]:
    if kind is None:
        return {}
    return {
        "kind": case((Order.kind_set_by == KIND_SET_BY_MODEL, kind), else_=Order.kind),
        "event_start": event_start, "event_end": event_end,
    }


async def hand_open_state_to_model(session: AsyncSession, order_id: uuid.UUID) -> None:
    """New mail is new evidence: whoever closed or reopened the order
    before, the model decides again at the next write."""
    await session.execute(
        update(Order).where(Order.id == order_id).values(open_set_by=OPEN_SET_BY_MODEL)
    )


async def update_controls(
    session: AsyncSession, order_id: uuid.UUID, *,
    is_favorite: bool | None = None, is_open: bool | None = None, is_sealed: bool | None = None,
    kind: str | None = None,
) -> bool:
    """
    Apply a person's change to an order's flags; None leaves a flag alone.
    Setting is_open or kind records that a person decided it, which a
    rewrite then leaves alone. Sealing never touches open/closed.

    Returns:
        False if the order does not exist
    """
    values: dict[str, Any] = {}
    if is_favorite is not None:
        values["is_favorite"] = is_favorite
    if is_sealed is not None:
        values["is_sealed"] = is_sealed
    if is_open is not None:
        values["is_open"] = is_open
        values["open_set_by"] = OPEN_SET_BY_USER
    if kind is not None:
        values["kind"] = kind
        values["kind_set_by"] = KIND_SET_BY_USER
    if not values:
        exists = await session.execute(select(Order.id).where(Order.id == order_id))
        return exists.scalar_one_or_none() is not None
    result = await session.execute(
        update(Order).where(Order.id == order_id).values(**values, updated_at=func.now())
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def mark_text_stale(session: AsyncSession, order_id: uuid.UUID) -> None:
    await session.execute(
        update(Order).where(Order.id == order_id).values(text_stale=True, updated_at=func.now())
    )


async def _release_order_jobs(session: AsyncSession, order_id: uuid.UUID) -> None:
    """Remove what the order_jobs table (which has no foreign key onto
    orders) still says about an order about to be deleted: its write jobs
    go, and a mail job's recorded outcome stops pointing at it. The mail
    job rows themselves stay -- they are the never-twice gate that keeps a
    catch-up from bundling the same mail again."""
    await session.execute(
        delete(OrderJob).where(OrderJob.kind == "write", OrderJob.order_id == order_id)
    )
    await session.execute(
        update(OrderJob)
        .where(OrderJob.kind == "mail", OrderJob.order_id == order_id)
        .values(order_id=None)
    )


async def delete_order_if_empty(session: AsyncSession, order_id: uuid.UUID) -> bool:
    """Delete an order left with no mail and no file. Returns whether it was deleted."""
    empty = await session.execute(
        select(Order.id).where(
            Order.id == order_id, Order.mail_count == 0, Order.file_count == 0,
        )
    )
    if empty.scalar_one_or_none() is None:
        return False
    return await delete_order(session, order_id)


async def detach_mail(
    session: AsyncSession, order_mail_id: uuid.UUID, *, move_documents_to: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """
    Remove one mail from its order. Its documents (and their code facts)
    follow the mail to `move_documents_to`, or are deleted when None.

    Returns:
        The order_id it was removed from, or None if the row was already
        gone
    """
    result = await session.execute(
        delete(OrderMail).where(OrderMail.id == order_mail_id).returning(
            OrderMail.order_id, OrderMail.account_id, OrderMail.msg_key,
        )
    )
    row = result.one_or_none()
    if row is None:
        return None
    mail_documents = select(OrderDocument.id).where(
        OrderDocument.source == "mail", OrderDocument.order_id == row.order_id,
        OrderDocument.account_id == row.account_id, OrderDocument.msg_key == row.msg_key,
    )
    if move_documents_to is None:
        await session.execute(delete(OrderDocument).where(OrderDocument.id.in_(mail_documents)))
    else:
        await _move_documents(session, mail_documents, move_documents_to)
    order_id: uuid.UUID = row.order_id
    return order_id


async def _move_documents(
    session: AsyncSession, document_ids: Any, target_id: uuid.UUID,
) -> None:
    """Re-parent documents and the code facts that point into them."""
    await session.execute(
        update(OrderFact).where(OrderFact.document_id.in_(document_ids)).values(order_id=target_id)
    )
    await session.execute(
        update(OrderDocument).where(OrderDocument.id.in_(document_ids)).values(order_id=target_id)
    )


async def merge_order(session: AsyncSession, *, source_id: uuid.UUID, target_id: uuid.UUID) -> None:
    """
    Merge `source_id` into `target_id`: every mail, document, file and
    identifier moves to the target, and the source is deleted. The source's
    display facts are dropped -- the target's rewrite writes them. The
    repair for a split the model made in error.
    """
    await session.execute(
        update(OrderMail).where(OrderMail.order_id == source_id).values(order_id=target_id)
    )
    source_documents = select(OrderDocument.id).where(OrderDocument.order_id == source_id)
    await _move_documents(session, source_documents, target_id)
    await session.execute(
        update(OrderFile).where(OrderFile.order_id == source_id).values(order_id=target_id)
    )
    source_identifiers = await session.execute(
        select(OrderFact.kind, OrderFact.label, OrderFact.value, OrderFact.value_norm).where(
            OrderFact.order_id == source_id, OrderFact.value_norm.is_not(None),
        )
    )
    for kind, label, value, value_norm in source_identifiers.all():
        next_position = (
            select(func.coalesce(func.max(OrderFact.position) + 1, 0))
            .where(OrderFact.order_id == target_id)
            .scalar_subquery()
        )
        stmt = (
            pg_insert(OrderFact)
            .values(
                order_id=target_id, kind=kind, label=label, value=value, value_norm=value_norm,
                position=next_position,
            )
            .on_conflict_do_nothing(
                index_elements=["order_id", "value_norm"],
                index_where=text("value_norm IS NOT NULL"),
            )
        )
        await session.execute(stmt)
    await delete_order(session, source_id)
    await recompute_aggregates(session, target_id)


async def delete_order(session: AsyncSession, order_id: uuid.UUID) -> bool:
    """Delete an order, its membership, facts, documents and files, and
    every job row that names it. No mail is touched, and none is released
    for bundling again."""
    await _release_order_jobs(session, order_id)
    result = await session.execute(delete(Order).where(Order.id == order_id))
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def delete_for_account(
    session: AsyncSession, account_id: uuid.UUID,
) -> list[tuple[uuid.UUID, str]]:
    """
    Account deletion's cleanup: remove this account's order_mails, their
    documents and order_jobs, delete orders left with no mail or file, and
    enqueue a write for orders that lost some but still have something
    else (mail from another account, or files).

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
    await session.execute(
        delete(OrderDocument).where(
            OrderDocument.source == "mail", OrderDocument.account_id == account_id,
        )
    )
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
