"""
Orders API endpoints.

GET    /api/orders                                  -- list, cursor paged
GET    /api/orders/{id}                              -- detail
DELETE /api/orders/{id}                              -- delete
POST   /api/orders/{id}/rewrite                      -- re-run the write call
POST   /api/orders/{id}/merge                        -- merge into another order
POST   /api/orders/{id}/mails/{mail_key}/detach       -- remove or move one mail
POST   /api/orders/catch-up                           -- sweep recent mail

An order is hidden from the list until it has been written once
(written_at is not null) -- see orders/repository.py's own write path.
`mail_key` in the detach route is order_mails.id, the row's own key, not
the durable msg_key text (see database/models.py's OrderMail docstring).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import and_, desc, or_, select

from mail_verdict.api.events import broadcast_event, get_event_ring
from mail_verdict.api.schemas import (
    OrderCatchUpRequest,
    OrderCatchUpResponse,
    OrderDetachRequest,
    OrderDetail,
    OrderDocumentOut,
    OrderIdentifierOut,
    OrderListItem,
    OrderListResponse,
    OrderMailOut,
    OrderMergeRequest,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Attachment, Order, OrderIdentifier, OrderMail
from mail_verdict.orders import repository
from mail_verdict.orders.catch_up import run_catch_up
from mail_verdict.orders.locate import resolve_mails
from mail_verdict.orders.text import compose_title
from mail_verdict.settings import get_settings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["orders"])

# Kept as attachments, not shown inline -- see docs/architecture.md's
# "Orders" section for why a ticket's inline QR image is dropped instead.
_DOCUMENT_CONTENT_TYPES = frozenset({
    "application/pdf", "application/vnd.apple.pkpass", "text/calendar", "application/ics",
})
_MAX_DOCUMENTS = 12


async def _account_ids_by_order(
    session: Any, order_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[uuid.UUID]]:
    if not order_ids:
        return {}
    result = await session.execute(
        select(OrderMail.order_id, OrderMail.account_id)
        .distinct()
        .where(OrderMail.order_id.in_(order_ids))
    )
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for order_id, account_id in result.all():
        out.setdefault(order_id, [])
        if account_id not in out[order_id]:
            out[order_id].append(account_id)
    return out


def _list_item(order: Order, account_ids: list[uuid.UUID]) -> OrderListItem:
    return OrderListItem(
        id=order.id, merchant=order.merchant, subject=order.subject, status=order.status,
        title=compose_title(subject=order.subject, status=order.status), is_open=order.is_open,
        icon=order.icon, summary_preview=order.summary_preview, first_mail_at=order.first_mail_at,
        last_mail_at=order.last_mail_at, mail_count=order.mail_count, account_ids=account_ids,
        text_stale=order.text_stale, updated_at=order.updated_at,
    )


@router.get("", response_model=OrderListResponse)
async def list_orders(
    state: str = Query(default="all", pattern="^(all|open)$"),
    before: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> OrderListResponse:
    """Orders across every enabled account, newest activity first."""
    db = get_db_connection()
    async with db.session() as session:
        cursor_last_mail_at, cursor_id = None, None
        if before is not None:
            cursor_result = await session.execute(
                select(Order.last_mail_at, Order.id).where(Order.id == before)
            )
            cursor_row = cursor_result.one_or_none()
            if cursor_row is None:
                raise HTTPException(
                    status_code=400, detail=f"Invalid cursor: order {before} not found",
                )
            cursor_last_mail_at, cursor_id = cursor_row

        stmt = select(Order).where(Order.written_at.is_not(None))
        if state == "open":
            stmt = stmt.where(Order.is_open.is_(True))
        if cursor_id is not None:
            stmt = stmt.where(
                or_(
                    Order.last_mail_at < cursor_last_mail_at,
                    and_(Order.last_mail_at == cursor_last_mail_at, Order.id < cursor_id),
                )
            )
        stmt = stmt.order_by(desc(Order.last_mail_at), desc(Order.id)).limit(limit + 1)

        rows = (await session.execute(stmt)).scalars().all()
        has_more = len(rows) > limit
        page = list(rows[:limit])
        account_ids = await _account_ids_by_order(session, [o.id for o in page])

    items = [_list_item(o, account_ids.get(o.id, [])) for o in page]
    next_cursor = str(items[-1].id) if has_more and items else None
    return OrderListResponse(items=items, has_more=has_more, next_cursor=next_cursor)


async def _load_detail(session: Any, order_id: uuid.UUID) -> OrderDetail | None:
    order_result = await session.execute(select(Order).where(Order.id == order_id))
    order = order_result.scalar_one_or_none()
    if order is None:
        return None

    account_ids = (await _account_ids_by_order(session, [order_id])).get(order_id, [])

    identifiers_result = await session.execute(
        select(OrderIdentifier.kind, OrderIdentifier.value).where(
            OrderIdentifier.order_id == order_id
        )
    )
    identifiers = [
        OrderIdentifierOut(kind=kind, value=value) for kind, value in identifiers_result.all()
    ]

    mails_result = await session.execute(
        select(OrderMail).where(OrderMail.order_id == order_id).order_by(OrderMail.received_at)
    )
    mails = list(mails_result.scalars().all())
    resolved = await resolve_mails(session, mails)

    mail_ids = [m.message_id for m in mails if m.message_id is not None]
    documents: list[OrderDocumentOut] = []
    if mail_ids:
        att_result = await session.execute(
            select(Attachment).where(
                Attachment.message_id.in_(mail_ids), Attachment.content_id.is_(None),
                Attachment.content_type.in_(_DOCUMENT_CONTENT_TYPES),
            )
        )
        received_by_message = {m.message_id: m.received_at for m in mails if m.message_id}
        for att in att_result.scalars().all():
            documents.append(
                OrderDocumentOut(
                    message_id=att.message_id, attachment_id=att.id,
                    filename=att.filename or "attachment", content_type=att.content_type or "",
                    size_bytes=att.size_bytes,
                    received_at=received_by_message.get(att.message_id, order.updated_at),
                )
            )
        documents.sort(key=lambda d: d.received_at, reverse=True)
        documents = documents[:_MAX_DOCUMENTS]

    mail_items = [
        OrderMailOut(
            key=m.id, account_id=m.account_id,
            message_id=resolved[m.id].message_id, thread_id=m.thread_id,
            location=resolved[m.id].location, folder_id=resolved[m.id].folder_id,
            is_seen=resolved[m.id].is_seen, subject=m.subject or "", from_addr=m.from_addr or "",
            received_at=m.received_at, attached_by=m.attached_by,
        )
        for m in mails
    ]

    base = _list_item(order, account_ids)
    return OrderDetail(
        **base.model_dump(), summary=order.summary, identifiers=identifiers,
        mails=mail_items, documents=documents,
    )


@router.get("/{order_id}", response_model=OrderDetail)
async def get_order(order_id: uuid.UUID) -> OrderDetail:
    db = get_db_connection()
    async with db.session() as session:
        detail = await _load_detail(session, order_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return detail


@router.delete("/{order_id}", status_code=204)
async def delete_order(order_id: uuid.UUID) -> None:
    """Delete an order. No mail is touched."""
    db = get_db_connection()
    async with db.session() as session:
        deleted = await repository.delete_order(session, order_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Order not found")
    await _announce(order_id, "deleted")


@router.post("/{order_id}/rewrite", status_code=202)
async def rewrite_order(order_id: uuid.UUID) -> None:
    """Enqueue a manual rewrite of an order's title and summary."""
    db = get_db_connection()
    async with db.session() as session:
        exists = await session.execute(select(Order.id).where(Order.id == order_id))
        if exists.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Order not found")
        await repository.enqueue_write_job(session, order_id, priority=10, origin="manual")


@router.post("/{order_id}/merge", response_model=OrderDetail)
async def merge_order(order_id: uuid.UUID, request: OrderMergeRequest) -> OrderDetail:
    """Merge `order_id` into `request.into`. The source disappears."""
    if order_id == request.into:
        raise HTTPException(status_code=400, detail="Cannot merge an order into itself")
    db = get_db_connection()
    async with db.session() as session:
        target_exists = await session.execute(select(Order.id).where(Order.id == request.into))
        if target_exists.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Target order not found")
        source_exists = await session.execute(select(Order.id).where(Order.id == order_id))
        if source_exists.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Order not found")
        await repository.merge_order(session, source_id=order_id, target_id=request.into)
        await repository.mark_text_stale(session, request.into)
        await repository.enqueue_write_job(session, request.into, priority=10, origin="manual")
        detail = await _load_detail(session, request.into)
    assert detail is not None
    await _announce(order_id, "deleted")
    await _announce(request.into, "updated")
    return detail


@router.post("/{order_id}/mails/{mail_key}/detach")
async def detach_mail(
    order_id: uuid.UUID, mail_key: uuid.UUID, request: OrderDetachRequest,
) -> OrderDetail | None:
    """Remove a mail from its order, optionally moving it to another --
    never bundled automatically again either way (uq_order_mails_account
    _msg_key holds; the job row for it is left as-is so a catch-up never
    reconsiders it)."""
    db = get_db_connection()
    async with db.session() as session:
        mail_result = await session.execute(
            select(OrderMail).where(OrderMail.id == mail_key, OrderMail.order_id == order_id)
        )
        mail = mail_result.scalar_one_or_none()
        if mail is None:
            raise HTTPException(status_code=404, detail="Mail not found in this order")

        if request.move_to is not None:
            target_exists = await session.execute(
                select(Order.id).where(Order.id == request.move_to)
            )
            if target_exists.scalar_one_or_none() is None:
                raise HTTPException(status_code=404, detail="Target order not found")

        removed_order_id = await repository.detach_mail(session, mail_key)
        assert removed_order_id == order_id

        if request.move_to is not None:
            await repository.attach_mail(
                session, order_id=request.move_to, account_id=mail.account_id,
                msg_key=mail.msg_key, message_id=mail.message_id, thread_id=mail.thread_id,
                subject=mail.subject, from_addr=mail.from_addr, received_at=mail.received_at,
                attached_by="user",
            )
            await repository.recompute_aggregates(session, request.move_to)
            await repository.mark_text_stale(session, request.move_to)
            await repository.enqueue_write_job(
                session, request.move_to, priority=10, origin="manual",
            )

        remaining = await repository.recompute_aggregates(session, order_id)
        order_deleted = False
        if remaining == 0:
            order_deleted = await repository.delete_order_if_empty(session, order_id)
        else:
            await repository.mark_text_stale(session, order_id)
            await repository.enqueue_write_job(session, order_id, priority=10, origin="manual")

        detail = None if order_deleted else await _load_detail(session, order_id)

    if request.move_to is not None:
        await _announce(request.move_to, "updated")
    await _announce(order_id, "deleted" if order_deleted else "updated")
    return detail


@router.post("/catch-up", response_model=OrderCatchUpResponse)
async def catch_up(request: OrderCatchUpRequest) -> OrderCatchUpResponse:
    db = get_db_connection()
    result = await run_catch_up(
        db, get_settings_service(), account_id=request.account_id, days=request.days,
        dry_run=request.dry_run,
    )
    return OrderCatchUpResponse(
        considered=result.considered, passed=result.passed, queued=result.queued,
    )


async def _announce(order_id: uuid.UUID, change: str) -> None:
    event_ring = get_event_ring()
    if event_ring is None:
        return
    db = get_db_connection()
    await broadcast_event(
        db, event_ring, "order.updated", {"order_id": str(order_id), "change": change},
    )
