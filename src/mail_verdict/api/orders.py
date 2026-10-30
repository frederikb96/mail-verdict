"""
Orders API endpoints.

GET    /api/orders                                  -- list, cursor paged (`before`), by kind
GET    /api/orders/{id}                              -- detail
PATCH  /api/orders/{id}                              -- favorite, open/closed, sealed, kind
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
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import Date, and_, cast, desc, func, or_, select

from mail_verdict.api.events import broadcast_event, get_event_ring
from mail_verdict.api.schemas import (
    OrderCatchUpRequest,
    OrderCatchUpResponse,
    OrderCodeOut,
    OrderDetachRequest,
    OrderDetail,
    OrderDocumentOut,
    OrderFactOut,
    OrderListItem,
    OrderListResponse,
    OrderMailOut,
    OrderMergeRequest,
    OrderUpdateRequest,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.fuzzy import (
    fuzzy_token_predicate,
    resolve_lexemes,
    set_word_similarity_threshold,
)
from mail_verdict.database.models import (
    Attachment,
    GlacierAttachment,
    Order,
    OrderDocument,
    OrderFact,
    OrderFile,
    OrderMail,
)
from mail_verdict.orders import repository
from mail_verdict.orders.carriers import tracking_url
from mail_verdict.orders.catch_up import run_catch_up
from mail_verdict.orders.locate import resolve_mails
from mail_verdict.orders.text import compose_title
from mail_verdict.settings import get_settings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["orders"])

_MAX_FILTER_LENGTH = 200

# Where an entry sits on the tickets timeline -- the one definition of the
# date rule, used to sort, to page and to answer.
TIMELINE_DATE = func.coalesce(
    Order.event_start, cast(func.timezone("UTC", Order.first_activity_at), Date),
)


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


def _list_item(
    order: Order, account_ids: list[uuid.UUID], timeline_date: date | None,
) -> OrderListItem:
    return OrderListItem(
        id=order.id, merchant=order.merchant, subject=order.subject, status=order.status,
        title=compose_title(subject=order.subject, status=order.status), is_open=order.is_open,
        is_favorite=order.is_favorite, is_sealed=order.is_sealed,
        open_set_by=order.open_set_by,  # type: ignore[arg-type]
        expected_until=order.expected_until, icon=order.icon,
        summary_preview=order.summary_preview,
        kind=order.kind,  # type: ignore[arg-type]
        kind_set_by=order.kind_set_by,  # type: ignore[arg-type]
        event_start=order.event_start, event_end=order.event_end, timeline_date=timeline_date,
        first_mail_at=order.first_mail_at, last_mail_at=order.last_mail_at,
        first_activity_at=order.first_activity_at, last_activity_at=order.last_activity_at,
        mail_count=order.mail_count, file_count=order.file_count, account_ids=account_ids,
        text_stale=order.text_stale, updated_at=order.updated_at,
    )


@router.get("", response_model=OrderListResponse)
async def list_orders(
    state: str = Query(default="all", pattern="^(all|open)$"),
    before: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    favorites: bool = Query(default=False),
    q: str | None = Query(default=None, max_length=_MAX_FILTER_LENGTH),
    kind: str = Query(default="all", pattern="^(all|ticket|order|other)$"),
    sort: str = Query(default="activity", pattern="^(activity|event)$"),
) -> OrderListResponse:
    """
    Orders across every enabled account, newest first.

    `sort=activity` (default) orders by last activity (a mail or a file);
    `sort=event` by `timeline_date` -- the event date of a ticket, else the
    date of its first activity -- newest date first. `before` is the id of
    the last entry of the previous page and continues in the same sort.
    `kind` keeps one kind. `favorites` keeps only favorites, in any state.
    `q` keeps orders where every token of it (any order, typo-tolerant)
    matches the merchant, subject, status or summary -- the same tokenizer
    and per-token predicate as the mail search's fallback tier.
    """
    db = get_db_connection()
    async with db.session() as session:
        sort_value = TIMELINE_DATE if sort == "event" else Order.last_activity_at
        cursor_value, cursor_id = None, None
        if before is not None:
            cursor_result = await session.execute(
                select(sort_value, Order.id).where(Order.id == before)
            )
            cursor_row = cursor_result.one_or_none()
            if cursor_row is None:
                raise HTTPException(
                    status_code=400, detail=f"Invalid cursor: order {before} not found",
                )
            cursor_value, cursor_id = cursor_row

        stmt = select(Order, TIMELINE_DATE.label("timeline_date")).where(
            Order.written_at.is_not(None)
        )
        if state == "open":
            stmt = stmt.where(Order.is_open.is_(True))
        if favorites:
            stmt = stmt.where(Order.is_favorite.is_(True))
        if kind != "all":
            stmt = stmt.where(Order.kind == kind)
        tokens = await resolve_lexemes(session, q) if q else []
        if tokens:
            await set_word_similarity_threshold(session)
            columns = (Order.merchant, Order.subject, Order.status, Order.summary)
            stmt = stmt.where(*[fuzzy_token_predicate(token, columns) for token in tokens])
        if cursor_id is not None:
            # Rows without a sort value (never written, so never listed) sort last.
            after_cursor = (
                or_(
                    sort_value < cursor_value, sort_value.is_(None),
                    and_(sort_value == cursor_value, Order.id < cursor_id),
                )
                if cursor_value is not None
                else and_(sort_value.is_(None), Order.id < cursor_id)
            )
            stmt = stmt.where(after_cursor)
        stmt = stmt.order_by(desc(sort_value).nulls_last(), desc(Order.id)).limit(limit + 1)

        rows = (await session.execute(stmt)).all()
        has_more = len(rows) > limit
        page = list(rows[:limit])
        account_ids = await _account_ids_by_order(session, [row.Order.id for row in page])

    items = [
        _list_item(row.Order, account_ids.get(row.Order.id, []), row.timeline_date)
        for row in page
    ]
    next_cursor = str(items[-1].id) if has_more and items else None
    return OrderListResponse(items=items, has_more=has_more, next_cursor=next_cursor)


async def _load_facts(session: Any, order_id: uuid.UUID) -> list[OrderFactOut]:
    result = await session.execute(
        select(
            OrderFact.id, OrderFact.kind, OrderFact.label, OrderFact.value, OrderFact.carrier,
            OrderFact.document_id, OrderFact.barcode_index,
        )
        .where(OrderFact.order_id == order_id)
        .order_by(OrderFact.position, OrderFact.created_at, OrderFact.id)
    )
    rows = result.all()

    # Barcodes of the documents that code facts point into -- the only
    # place the (sizeable) barcodes column is read.
    document_ids = {row.document_id for row in rows if row.document_id is not None}
    barcodes: dict[uuid.UUID, dict[int, dict[str, Any]]] = {}
    if document_ids:
        barcode_result = await session.execute(
            select(OrderDocument.id, OrderDocument.barcodes).where(
                OrderDocument.id.in_(document_ids)
            )
        )
        for document_id, entries in barcode_result.all():
            barcodes[document_id] = {
                entry["index"]: entry for entry in entries if "index" in entry
            }

    facts: list[OrderFactOut] = []
    for row in rows:
        code = None
        if row.kind == "code" and row.document_id is not None:
            entry = barcodes.get(row.document_id, {}).get(row.barcode_index)
            if entry is not None:
                payload = entry.get("text")
                code = OrderCodeOut(
                    format=str(entry.get("format", "")),
                    payload_text=payload if isinstance(payload, str) and payload.isprintable()
                    else None,
                    image_url=f"/api/orders/{order_id}/codes/{row.id}.png",
                    page=entry.get("page"),
                )
        facts.append(
            OrderFactOut(
                id=row.id, kind=row.kind, label=row.label, value=row.value, carrier=row.carrier,
                tracking_url=(
                    tracking_url(row.carrier, row.value) if row.kind == "tracking_number" else None
                ),
                code=code, document_id=row.document_id,
            )
        )
    return facts


async def _mail_attachment_urls(
    session: Any, documents: list[Any], message_by_mail: dict[tuple[uuid.UUID, str], uuid.UUID],
) -> dict[uuid.UUID, str]:
    """Download URLs of the mail documents, by document id: each mail
    document is matched to an attachment of where its mail is now -- the
    stored attachment id first, else the same filename and type (a resync
    or a move into the glacier gives a mail's attachments new ids)."""
    message_ids = set(message_by_mail.values())
    if not message_ids:
        return {}
    candidates: dict[uuid.UUID, list[Any]] = {}
    live = await session.execute(
        select(
            Attachment.id, Attachment.message_id, Attachment.filename, Attachment.content_type,
        ).where(Attachment.message_id.in_(message_ids))
    )
    for row in live.all():
        candidates.setdefault(row.message_id, []).append(row)
    glacier = await session.execute(
        select(
            GlacierAttachment.id, GlacierAttachment.glacier_message_id.label("message_id"),
            GlacierAttachment.filename, GlacierAttachment.content_type,
        ).where(GlacierAttachment.glacier_message_id.in_(message_ids))
    )
    for row in glacier.all():
        candidates.setdefault(row.message_id, []).append(row)

    urls: dict[uuid.UUID, str] = {}
    for document in documents:
        if document.source != "mail":
            continue
        message_id = message_by_mail.get((document.account_id, document.msg_key))
        attachments = candidates.get(message_id, []) if message_id is not None else []
        match = next((a for a in attachments if a.id == document.attachment_id), None) or next(
            (
                a for a in attachments
                if (a.filename or "") == document.filename
                and (a.content_type or "") == document.content_type
            ),
            None,
        )
        if match is not None:
            urls[document.id] = f"/api/messages/{message_id}/attachments/{match.id}"
    return urls


async def _load_documents(
    session: Any, order_id: uuid.UUID, mails: list[OrderMail], resolved: dict[uuid.UUID, Any],
) -> list[OrderDocumentOut]:
    # Never selects text or barcodes: a detail view shows neither.
    result = await session.execute(
        select(
            OrderDocument.id, OrderDocument.source, OrderDocument.account_id,
            OrderDocument.msg_key, OrderDocument.attachment_id, OrderDocument.file_id,
            OrderDocument.filename, OrderDocument.content_type, OrderDocument.size_bytes,
            OrderDocument.page_count, OrderDocument.method,
            func.jsonb_array_length(OrderDocument.barcodes).label("barcode_count"),
            func.coalesce(
                OrderMail.received_at, OrderFile.created_at, OrderDocument.extracted_at,
            ).label("received_at"),
        )
        .outerjoin(
            OrderMail,
            and_(
                OrderDocument.source == "mail",
                OrderMail.account_id == OrderDocument.account_id,
                OrderMail.msg_key == OrderDocument.msg_key,
            ),
        )
        .outerjoin(OrderFile, OrderFile.id == OrderDocument.file_id)
        .where(OrderDocument.order_id == order_id)
        .order_by(desc("received_at"), OrderDocument.filename, OrderDocument.id)
    )
    rows = result.all()

    message_by_mail = {
        (m.account_id, m.msg_key): resolved[m.id].message_id
        for m in mails if resolved[m.id].message_id is not None
    }
    mail_urls = await _mail_attachment_urls(session, rows, message_by_mail)

    documents: list[OrderDocumentOut] = []
    for row in rows:
        if row.source == "file":
            download_url: str | None = f"/api/orders/{order_id}/files/{row.file_id}"
            message_id = None
        else:
            download_url = mail_urls.get(row.id)
            message_id = message_by_mail.get((row.account_id, row.msg_key))
        documents.append(
            OrderDocumentOut(
                id=row.id, source=row.source, message_id=message_id,
                attachment_id=row.attachment_id, file_id=row.file_id, filename=row.filename,
                content_type=row.content_type, size_bytes=row.size_bytes,
                received_at=row.received_at, page_count=row.page_count, method=row.method,
                barcode_count=row.barcode_count, download_url=download_url,
            )
        )
    return documents


async def _load_detail(session: Any, order_id: uuid.UUID) -> OrderDetail | None:
    order_result = await session.execute(
        select(Order, TIMELINE_DATE.label("timeline_date")).where(Order.id == order_id)
    )
    order_row = order_result.one_or_none()
    if order_row is None:
        return None
    order = order_row.Order

    account_ids = (await _account_ids_by_order(session, [order_id])).get(order_id, [])
    facts = await _load_facts(session, order_id)

    mails_result = await session.execute(
        select(OrderMail).where(OrderMail.order_id == order_id).order_by(OrderMail.received_at)
    )
    mails = list(mails_result.scalars().all())
    resolved = await resolve_mails(session, mails)
    documents = await _load_documents(session, order_id, mails, resolved)

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

    base = _list_item(order, account_ids, order_row.timeline_date)
    return OrderDetail(
        **base.model_dump(), summary=order.summary, facts=facts,
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


@router.patch("/{order_id}", response_model=OrderDetail)
async def update_order(order_id: uuid.UUID, request: OrderUpdateRequest) -> OrderDetail:
    """Favorite, open/closed, sealed and kind. A person's open/closed
    decision sticks until the next mail arrives; a person's kind sticks for
    good (see orders/repository.py)."""
    db = get_db_connection()
    async with db.session() as session:
        updated = await repository.update_controls(
            session, order_id, is_favorite=request.is_favorite, is_open=request.is_open,
            is_sealed=request.is_sealed, kind=request.kind,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Order not found")
        detail = await _load_detail(session, order_id)
    assert detail is not None
    await _announce(order_id, "updated")
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


@router.post("/{order_id}/mails/{mail_key}/detach", response_model=None)
async def detach_mail(
    order_id: uuid.UUID, mail_key: uuid.UUID, request: OrderDetachRequest,
) -> OrderDetail | Response:
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

        removed_order_id = await repository.detach_mail(
            session, mail_key, move_documents_to=request.move_to,
        )
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
    if detail is None:
        return Response(status_code=204)
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
