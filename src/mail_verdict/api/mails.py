"""
Message API endpoints.

GET /api/accounts/:account_id/messages — cursor-paginated list, optionally
  grouped into one row per conversation (threaded=true)
GET /api/messages/:id — detail view (sanitized HTML, embedded verdict)
GET /api/messages/:id/thread — every message in the conversation, ascending
GET /api/messages/:id/attachments/:attachment_id — streamed attachment
GET /api/messages/:id/raw — the message's RFC822 source as a .eml download
POST /api/messages/:id/action — single-message action
POST /api/accounts/:account_id/messages/bulk-action — action over many
  messages, by id list or by a server-resolved scope

PostIMAP integration: SQL UPDATEs are sufficient for all actions --
postimap/actions.py owns the write shapes; PostIMAP's own triggers
propagate them to IMAP, and postimap/listener.py fans them out to SSE.
"""

from __future__ import annotations

import html
import logging
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import all_, any_, case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, defer

from mail_verdict.api.deps import (
    get_attachment_repo,
    get_tag_repo,
    get_verdict_repo,
)
from mail_verdict.api.image_exceptions import is_sender_image_allowed
from mail_verdict.api.schemas import (
    AttachmentSummary,
    BulkActionRequest,
    BulkActionResponse,
    BulkActionScope,
    MessageActionRequest,
    MessageActionResponse,
    MessageDetail,
    MessageListResponse,
    MessageQuoteResponse,
    MessageSummary,
    SelectionSnapshotResponse,
    TagResponse,
    ThreadResponse,
    VerdictResponse,
)
from mail_verdict.core.content_disposition import content_disposition
from mail_verdict.core.cursor import after_cursor, before_cursor
from mail_verdict.core.image_sanitizer import restore_remote_images, strip_remote_images
from mail_verdict.core.outbound_sanitizer import sanitize_outbound_html
from mail_verdict.core.sanitizer import (
    rewrite_remote_images,
    sanitize_email_html,
)
from mail_verdict.database.connection import DatabaseConnection, get_db_connection
from mail_verdict.database.models import Attachment, Folder, Message
from mail_verdict.postimap.actions import (
    expunge,
    expunge_bulk,
    move_message,
    move_message_bulk,
    move_to_trash,
    set_flags,
    set_flags_bulk,
    set_keywords,
)

logger = logging.getLogger(__name__)

# Detail/thread/action/attachment routes: /api/messages/...
router = APIRouter(prefix="/messages", tags=["messages"])

# List + bulk-action routes: /api/accounts/{account_id}/messages...
account_router = APIRouter(prefix="/accounts/{account_id}/messages", tags=["messages"])

# A list row (MessageSummary) never renders these -- deferring them keeps a
# page of the list from pulling the full raw message and its HTML body
# across the wire for every row just to read a sender and a subject.
# body_text stays eager: the list snippet reads the first 120 characters of
# it, and a deferred attribute accessed outside the session's own greenlet
# raises MissingGreenlet rather than lazy-loading, the way it would sync.
_LIST_DEFERRED_COLUMNS = (
    defer(Message.raw_source),
    defer(Message.raw_headers),
    defer(Message.body_html),
)

# MessageDetail renders body_text/body_html but never raw_source (the
# entire RFC822 bytea, its own GET .../raw endpoint) or raw_headers (used
# only by the pipeline's MessageView, never by this response). Without
# this, get_message and get_thread pull the full raw message for every
# row just to produce about a kilobyte of JSON -- the thread endpoint
# doing it once per message in the conversation.
_DETAIL_DEFERRED_COLUMNS = (
    defer(Message.raw_source),
    defer(Message.raw_headers),
)


def _flat_summary(m: Message) -> MessageSummary:
    return MessageSummary(
        id=m.id,
        account_id=m.account_id,
        folder_id=m.folder_id,
        thread_id=m.thread_id,
        subject=m.subject,
        from_addr=m.from_addr,
        to_addrs=m.to_addrs,
        received_at=m.received_at,
        is_seen=m.is_seen,
        is_flagged=m.is_flagged,
        is_answered=m.is_answered,
        is_draft=m.is_draft,
        is_truncated=m.is_truncated,
        pending_sync=m.imap_uid is None,
        snippet=m.body_text[:120] if m.body_text else None,
        mirrored_at=m.created_at,
    )


def _threaded_summary(m: Message, thread_count: int, unread_in_thread: int) -> MessageSummary:
    return MessageSummary(
        id=m.id,
        account_id=m.account_id,
        folder_id=m.folder_id,
        thread_id=m.thread_id,
        subject=m.subject,
        from_addr=m.from_addr,
        to_addrs=m.to_addrs,
        received_at=m.received_at,
        is_seen=m.is_seen,
        is_flagged=m.is_flagged,
        is_answered=m.is_answered,
        is_draft=m.is_draft,
        is_truncated=m.is_truncated,
        pending_sync=m.imap_uid is None,
        snippet=m.body_text[:120] if m.body_text else None,
        thread_count=thread_count,
        unread_in_thread=unread_in_thread,
        mirrored_at=m.created_at,
    )


@account_router.get("", response_model=MessageListResponse)
async def list_messages(
    account_id: uuid.UUID,
    folder_id: uuid.UUID | None = Query(default=None),
    threaded: bool = Query(default=False, description="One row per conversation"),
    is_seen: bool | None = Query(default=None),
    since: datetime | None = Query(default=None),
    before: uuid.UUID | None = Query(
        default=None,
        description="Cursor: UUID of last message in previous page -- fetches older",
    ),
    after: uuid.UUID | None = Query(
        default=None,
        description="Cursor: UUID of first message in previous page -- fetches newer",
    ),
    around: uuid.UUID | None = Query(
        default=None,
        description=(
            "Centre a fresh page on this message instead of starting at the newest "
            "edge -- half newer, half older. In threaded mode the target is resolved "
            "to its thread's own representative row first. Mutually exclusive with "
            "before/after."
        ),
    ),
    limit: int = Query(default=50, ge=1, le=200),
) -> MessageListResponse:
    """
    List messages with cursor-based pagination.

    threaded=true groups by thread_id: one row per conversation (its latest
    message, plus thread_count/unread_in_thread scoped to this same folder
    filter), ordered by that latest message's received_at.

    Three ways to page. The plain call starts at the newest edge. `before`
    continues older from a previous page's last row, exactly as it always
    has. `after` continues newer from a previous page's first row -- only
    meaningful once a page was ever centred away from the edge, since an
    ordinary page already starts there and has nothing newer to fetch.
    `around` centres a *fresh* page on a given message instead of the
    newest edge -- half newer, half older -- resolving it to its thread's
    own representative row first in threaded mode (the latest message in
    its thread among those matching this list's own filters), since that
    is the row the list actually renders; centring on the message itself
    would return a window the list never shows. 404 if the message
    doesn't exist, isn't in this account, or -- threaded -- its thread has
    no member matching folder_id/is_seen/since here: "not a member of
    this list" is a distinct answer from an ordinary empty page.
    """
    if around is not None and (before is not None or after is not None):
        raise HTTPException(
            status_code=400, detail="around is mutually exclusive with before/after",
        )

    db = get_db_connection()
    async with db.session() as session:
        if around is not None:
            return await _list_messages_around(
                session, account_id, folder_id, threaded, is_seen, since, around, limit,
            )

        direction: Literal["older", "newer"] = "newer" if after is not None else "older"
        cursor_param = after if after is not None else before
        cursor_received_at, cursor_id = None, None
        if cursor_param is not None:
            cursor_result = await session.execute(
                select(Message.received_at, Message.id).where(Message.id == cursor_param)
            )
            cursor_row = cursor_result.one_or_none()
            if cursor_row is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid cursor: message {cursor_param} not found",
                )
            cursor_received_at, cursor_id = cursor_row

        if threaded:
            rows = await _list_messages_threaded(
                session, account_id, folder_id, is_seen, since,
                cursor_received_at, cursor_id, limit, direction=direction,
            )
            overflow = len(rows) > limit
            page = rows[:limit]
            if direction == "newer":
                page = list(reversed(page))
            messages = [_threaded_summary(m, tc, uc) for m, tc, uc in page]
        else:
            all_msgs = await _list_messages_flat_page(
                session, account_id, folder_id, is_seen, since,
                cursor_received_at, cursor_id, limit, direction=direction,
            )
            overflow = len(all_msgs) > limit
            page_msgs = all_msgs[:limit]
            if direction == "newer":
                page_msgs = list(reversed(page_msgs))
            messages = [_flat_summary(m) for m in page_msgs]

        # Only the direction actually explored by this fetch is a genuinely
        # open question; the other stays at its safe default (nothing more)
        # since an ordinary single-directional page never needs the server
        # to answer it -- see the has_more_newer/prev_cursor field docs.
        has_more = overflow if direction == "older" else False
        has_more_newer = overflow if direction == "newer" else False

    next_cursor = str(messages[-1].id) if has_more and messages else None
    prev_cursor = str(messages[0].id) if has_more_newer and messages else None
    return MessageListResponse(
        messages=messages, has_more=has_more, next_cursor=next_cursor,
        has_more_newer=has_more_newer, prev_cursor=prev_cursor,
    )


async def _list_messages_around(
    session: AsyncSession,
    account_id: uuid.UUID,
    folder_id: uuid.UUID | None,
    threaded: bool,
    is_seen: bool | None,
    since: datetime | None,
    around: uuid.UUID,
    limit: int,
) -> MessageListResponse:
    """A page centred on `around` rather than the newest edge -- see
    list_messages's own docstring for the threaded resolution and the
    not-a-member answer."""
    if threaded:
        resolved = await _resolve_around_threaded(
            session, account_id, folder_id, is_seen, since, around,
        )
    else:
        flat_target = await _resolve_around_flat(
            session, account_id, folder_id, is_seen, since, around,
        )
        resolved = (flat_target, 0, 0) if flat_target is not None else None

    if resolved is None:
        raise HTTPException(
            status_code=404, detail=f"Message {around} is not a member of this list",
        )
    target, target_thread_count, target_unread_in_thread = resolved

    # What's left after the target's own row is split roughly evenly; an
    # odd remainder goes to the newer half, since catching up to a live
    # tail is the direction most likely to matter again soon.
    remaining = max(limit - 1, 0)
    half_older = remaining // 2
    half_newer = remaining - half_older

    if threaded:
        older_rows = await _list_messages_threaded(
            session, account_id, folder_id, is_seen, since,
            target.received_at, target.id, half_older, direction="older",
        )
        newer_rows = await _list_messages_threaded(
            session, account_id, folder_id, is_seen, since,
            target.received_at, target.id, half_newer, direction="newer",
        )
        has_more = len(older_rows) > half_older
        has_more_newer = len(newer_rows) > half_newer
        combined = [
            *reversed(newer_rows[:half_newer]),
            (target, target_thread_count, target_unread_in_thread),
            *older_rows[:half_older],
        ]
        messages = [_threaded_summary(m, tc, uc) for m, tc, uc in combined]
    else:
        older_msgs = await _list_messages_flat_page(
            session, account_id, folder_id, is_seen, since,
            target.received_at, target.id, half_older, direction="older",
        )
        newer_msgs = await _list_messages_flat_page(
            session, account_id, folder_id, is_seen, since,
            target.received_at, target.id, half_newer, direction="newer",
        )
        has_more = len(older_msgs) > half_older
        has_more_newer = len(newer_msgs) > half_newer
        combined_msgs = [*reversed(newer_msgs[:half_newer]), target, *older_msgs[:half_older]]
        messages = [_flat_summary(m) for m in combined_msgs]

    next_cursor = str(messages[-1].id) if has_more and messages else None
    prev_cursor = str(messages[0].id) if has_more_newer and messages else None
    return MessageListResponse(
        messages=messages, has_more=has_more, next_cursor=next_cursor,
        has_more_newer=has_more_newer, prev_cursor=prev_cursor,
    )


async def _resolve_around_flat(
    session: AsyncSession,
    account_id: uuid.UUID,
    folder_id: uuid.UUID | None,
    is_seen: bool | None,
    since: datetime | None,
    around: uuid.UUID,
) -> Message | None:
    """The `around` target itself, if it matches this list's own filters --
    None otherwise (it doesn't exist, isn't in this account, or is filtered
    out), which the caller reports as "not a member of this list"."""
    stmt = (
        select(Message)
        .options(*_LIST_DEFERRED_COLUMNS)
        .where(
            Message.id == around, Message.account_id == account_id,
            Message.expunged_at.is_(None),
        )
    )
    if folder_id is not None:
        stmt = stmt.where(Message.folder_id == folder_id)
    if is_seen is not None:
        stmt = stmt.where(Message.is_seen == is_seen)
    if since is not None:
        stmt = stmt.where(Message.received_at >= since)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _resolve_around_threaded(
    session: AsyncSession,
    account_id: uuid.UUID,
    folder_id: uuid.UUID | None,
    is_seen: bool | None,
    since: datetime | None,
    around: uuid.UUID,
) -> tuple[Message, int, int] | None:
    """Resolve `around` to the row that actually represents it in threaded
    mode: the latest message in its own thread among those matching this
    list's filters -- never `around` itself, which the list may not even
    show (its thread's newest row, possibly a different message, is what
    a threaded list renders). None means the thread has no member matching
    the filters at all -- a thread existing is not enough, since every one
    of its messages could still be filtered out (e.g. an unread-only view
    where this thread is fully read)."""
    thread_result = await session.execute(
        select(Message.thread_id).where(
            Message.id == around, Message.account_id == account_id,
            Message.expunged_at.is_(None),
        )
    )
    thread_id = thread_result.scalar_one_or_none()
    if thread_id is None:
        return None

    filters = [
        Message.account_id == account_id, Message.expunged_at.is_(None),
        Message.thread_id == thread_id,
    ]
    if folder_id is not None:
        filters.append(Message.folder_id == folder_id)
    if is_seen is not None:
        filters.append(Message.is_seen == is_seen)
    if since is not None:
        filters.append(Message.received_at >= since)

    representative_result = await session.execute(
        select(Message)
        .options(*_LIST_DEFERRED_COLUMNS)
        .where(*filters)
        .order_by(desc(Message.received_at), desc(Message.id))
        .limit(1)
    )
    representative = representative_result.scalar_one_or_none()
    if representative is None:
        return None

    stats_result = await session.execute(
        select(
            func.count(Message.id),
            func.count(case((Message.is_seen.is_(False), Message.id))),
        ).where(*filters)
    )
    thread_count, unread_in_thread = stats_result.one()
    return representative, thread_count, unread_in_thread


async def _list_messages_flat_page(
    session: AsyncSession,
    account_id: uuid.UUID,
    folder_id: uuid.UUID | None,
    is_seen: bool | None,
    since: datetime | None,
    cursor_received_at: datetime | None,
    cursor_id: uuid.UUID | None,
    limit: int,
    *,
    direction: Literal["older", "newer"] = "older",
) -> list[Message]:
    """
    One page of ordinary (non-threaded) messages.

    "older" (the default, and the only direction used before `around`
    existed) is the ordinary continue-scrolling-down case, fetched newest
    first. "newer" is its mirror, used to grow a window that opened away
    from the newest edge back up toward it -- fetched oldest-of-the-newer
    first (closest to the cursor), since a keyset predicate can only walk
    forward from its cursor; the caller reverses it before rendering, since
    the list itself is always newest-first regardless of which direction a
    given page happened to be fetched in.
    """
    stmt = (
        select(Message)
        .options(*_LIST_DEFERRED_COLUMNS)
        .where(Message.expunged_at.is_(None), Message.account_id == account_id)
    )
    if folder_id is not None:
        stmt = stmt.where(Message.folder_id == folder_id)
    if is_seen is not None:
        stmt = stmt.where(Message.is_seen == is_seen)
    if since is not None:
        stmt = stmt.where(Message.received_at >= since)

    if direction == "older":
        stmt = stmt.order_by(desc(Message.received_at), desc(Message.id))
        if cursor_id is not None:
            stmt = stmt.where(
                after_cursor(Message.received_at, Message.id, cursor_received_at, cursor_id)
            )
    else:
        stmt = stmt.order_by(Message.received_at.asc(), Message.id.asc())
        if cursor_id is not None:
            stmt = stmt.where(
                before_cursor(Message.received_at, Message.id, cursor_received_at, cursor_id)
            )
    stmt = stmt.limit(limit + 1)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _list_messages_threaded(
    session: AsyncSession,
    account_id: uuid.UUID,
    folder_id: uuid.UUID | None,
    is_seen: bool | None,
    since: datetime | None,
    cursor_received_at: datetime | None,
    cursor_id: uuid.UUID | None,
    limit: int,
    *,
    direction: Literal["older", "newer"] = "older",
) -> list[tuple[Message, int, int]]:
    """
    One row per thread_id: the latest message plus its thread's counts.

    Postgres DISTINCT ON picks the latest message per thread_id (ties broken
    by id); that result is joined against a per-thread aggregate (same
    filters) for thread_count/unread_in_thread, then re-ordered by
    received_at for cursor pagination -- DISTINCT ON's own ORDER BY must
    start with thread_id, so the "latest thread first" order has to be
    applied as an outer step, not folded into the same ORDER BY.

    Both the DISTINCT ON pick and the count aggregate are scoped to the
    same folder filter as the rest of the list: a thread's count here means
    "messages in this thread within this folder", matching the per-folder
    browsing the list itself is scoped to.

    direction: see _list_messages_flat_page's own docstring -- the same
    "older" default / "newer" mirror, and the same reversal obligation on
    the caller.
    """
    filters = [Message.account_id == account_id, Message.expunged_at.is_(None)]
    if folder_id is not None:
        filters.append(Message.folder_id == folder_id)
    if is_seen is not None:
        filters.append(Message.is_seen == is_seen)
    if since is not None:
        filters.append(Message.received_at >= since)

    latest_per_thread = (
        select(Message)
        .where(*filters)
        .distinct(Message.thread_id)
        .order_by(Message.thread_id, desc(Message.received_at), desc(Message.id))
        .subquery("latest_per_thread")
    )
    latest = aliased(Message, latest_per_thread)

    thread_stats = (
        select(
            Message.thread_id.label("thread_id"),
            func.count(Message.id).label("thread_count"),
            func.count(case((Message.is_seen.is_(False), Message.id))).label("unread_in_thread"),
        )
        .where(*filters)
        .group_by(Message.thread_id)
        .subquery("thread_stats")
    )

    stmt = (
        select(latest, thread_stats.c.thread_count, thread_stats.c.unread_in_thread)
        .join(thread_stats, thread_stats.c.thread_id == latest.thread_id)
    )
    if direction == "older":
        stmt = stmt.order_by(desc(latest.received_at), desc(latest.id))
        if cursor_id is not None:
            stmt = stmt.where(
                after_cursor(latest.received_at, latest.id, cursor_received_at, cursor_id)
            )
    else:
        stmt = stmt.order_by(latest.received_at.asc(), latest.id.asc())
        if cursor_id is not None:
            stmt = stmt.where(
                before_cursor(latest.received_at, latest.id, cursor_received_at, cursor_id)
            )
    stmt = stmt.limit(limit + 1)

    result = await session.execute(stmt)
    return [(row[0], row.thread_count, row.unread_in_thread) for row in result.all()]


@router.get("/{message_id}", response_model=MessageDetail)
async def get_message(
    message_id: uuid.UUID,
    load_images: bool = Query(default=False, description="Load remote images if allowed"),
) -> MessageDetail:
    """
    Get full message detail by ID.

    Returns the message with attachments, tags, the current verdict (if
    any), and image privacy controls. HTML is sanitized here at read time
    -- PostIMAP owns the insert, so body_html is untrusted until this pass.
    """
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Message).options(*_DETAIL_DEFERRED_COLUMNS).where(Message.id == message_id)
        )
        msg = result.scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")

    tag_repo = get_tag_repo()
    tags = await tag_repo.get_tags_for_mail(message_id)

    attachment_repo = get_attachment_repo()
    attachments = await attachment_repo.get_by_message_id(message_id)

    verdict_repo = get_verdict_repo()
    verdict = await verdict_repo.get_latest_for_mail(message_id)

    body_html = msg.body_html
    images_allowed = False
    has_blocked_images = False
    if body_html:
        body_html = sanitize_email_html(body_html)
        body_html = _rewrite_cid_references(body_html, message_id, attachments)

        images_allowed = await is_sender_image_allowed(msg.account_id, msg.from_addr)

        if images_allowed and load_images:
            body_html = restore_remote_images(body_html)
            has_blocked_images = False
        else:
            body_html, has_blocked_images = strip_remote_images(body_html)

    return MessageDetail(
        id=msg.id,
        account_id=msg.account_id,
        folder_id=msg.folder_id,
        thread_id=msg.thread_id,
        pending_sync=msg.imap_uid is None,
        is_truncated=msg.is_truncated,
        message_id=msg.message_id,
        subject=msg.subject,
        from_addr=msg.from_addr,
        to_addrs=msg.to_addrs,
        cc_addrs=msg.cc_addrs,
        bcc_addrs=msg.bcc_addrs,
        reply_to=msg.reply_to,
        in_reply_to=msg.in_reply_to,
        references=msg.msg_references,
        body_text=msg.body_text,
        body_html=body_html,
        received_at=msg.received_at,
        size_bytes=msg.size_bytes,
        is_seen=msg.is_seen,
        is_flagged=msg.is_flagged,
        is_answered=msg.is_answered,
        is_draft=msg.is_draft,
        keywords=msg.keywords or [],
        snippet=msg.body_text[:120] if msg.body_text else None,
        created_at=msg.created_at,
        has_blocked_images=has_blocked_images,
        images_allowed=images_allowed,
        tags=[TagResponse(tag_name=t.tag_name, source=t.source.value) for t in tags],
        attachments=[
            AttachmentSummary(
                id=a.id, filename=a.filename, content_type=a.content_type, size_bytes=a.size_bytes,
            )
            for a in attachments
        ],
        verdict=(
            VerdictResponse(
                id=verdict.id, message_id=verdict.mail_id, is_spam=verdict.is_spam,
                model_used=verdict.model_used, reasoning=verdict.reasoning,
                source=verdict.source.value, created_at=verdict.created_at,
            )
            if verdict
            else None
        ),
    )


def _rewrite_cid_references(
    body_html: str, message_id: uuid.UUID, attachments: list[Attachment],
) -> str:
    """
    Rewrite cid: image sources to the attachment streaming endpoint.

    Inline images referenced by Content-ID resolve through
    GET /messages/{id}/attachments/{attachment_id} rather than staying as
    a cid: URI the browser cannot fetch directly.
    """
    import re

    by_content_id = {a.content_id.strip("<>"): a.id for a in attachments if a.content_id}
    if not by_content_id:
        return body_html

    def _replace(match: re.Match[str]) -> str:
        cid = match.group(2)
        attachment_id = by_content_id.get(cid)
        if attachment_id is None:
            return match.group(0)
        return f'{match.group(1)}/api/messages/{message_id}/attachments/{attachment_id}"'

    return re.sub(r'(\bsrc\s*=\s*")cid:([^"]+)"', _replace, body_html, flags=re.IGNORECASE)


@router.get("/{message_id}/thread", response_model=ThreadResponse)
async def get_thread(message_id: uuid.UUID) -> ThreadResponse:
    """
    Get every message in this message's conversation, across folders, ascending.

    This is how a Sent reply appears inside the thread it belongs to --
    thread_id groups across folders, not just within the one the anchor
    message happens to be in.
    """
    db = get_db_connection()
    async with db.session() as session:
        anchor = await session.execute(select(Message.thread_id).where(Message.id == message_id))
        thread_id = anchor.scalar_one_or_none()
        if thread_id is None:
            raise HTTPException(status_code=404, detail="Message not found")

        result = await session.execute(
            select(Message)
            .options(*_DETAIL_DEFERRED_COLUMNS)
            .where(Message.thread_id == thread_id, Message.expunged_at.is_(None))
            .order_by(Message.received_at)
        )
        thread_messages = list(result.scalars().all())

        tag_repo = get_tag_repo()
        attachment_repo = get_attachment_repo()
        verdict_repo = get_verdict_repo()

        details: list[MessageDetail] = []
        for m in thread_messages:
            tags = await tag_repo.get_tags_for_mail(m.id)
            attachments = await attachment_repo.get_by_message_id(m.id)
            verdict = await verdict_repo.get_latest_for_mail(m.id)

            body_html = m.body_html
            if body_html:
                body_html = sanitize_email_html(body_html)
                body_html = _rewrite_cid_references(body_html, m.id, attachments)
                images_allowed = await is_sender_image_allowed(m.account_id, m.from_addr)
                body_html, has_blocked = (
                    (restore_remote_images(body_html), False)
                    if images_allowed
                    else strip_remote_images(body_html)
                )
            else:
                images_allowed, has_blocked = False, False

            details.append(
                MessageDetail(
                    id=m.id, account_id=m.account_id, folder_id=m.folder_id,
                    thread_id=m.thread_id, pending_sync=m.imap_uid is None,
                    is_truncated=m.is_truncated, message_id=m.message_id,
                    subject=m.subject, from_addr=m.from_addr, to_addrs=m.to_addrs,
                    cc_addrs=m.cc_addrs, bcc_addrs=m.bcc_addrs, reply_to=m.reply_to,
                    in_reply_to=m.in_reply_to, references=m.msg_references,
                    body_text=m.body_text, body_html=body_html,
                    received_at=m.received_at, size_bytes=m.size_bytes,
                    is_seen=m.is_seen, is_flagged=m.is_flagged, is_answered=m.is_answered,
                    is_draft=m.is_draft, keywords=m.keywords or [],
                    snippet=m.body_text[:120] if m.body_text else None,
                    created_at=m.created_at, has_blocked_images=has_blocked,
                    images_allowed=images_allowed,
                    tags=[TagResponse(tag_name=t.tag_name, source=t.source.value) for t in tags],
                    attachments=[
                        AttachmentSummary(
                            id=a.id, filename=a.filename,
                            content_type=a.content_type, size_bytes=a.size_bytes,
                        )
                        for a in attachments
                    ],
                    verdict=(
                        VerdictResponse(
                            id=verdict.id, message_id=verdict.mail_id, is_spam=verdict.is_spam,
                            model_used=verdict.model_used, reasoning=verdict.reasoning,
                            source=verdict.source.value, created_at=verdict.created_at,
                        )
                        if verdict
                        else None
                    ),
                )
            )
        return ThreadResponse(messages=details)


@router.get("/{message_id}/attachments/{attachment_id}")
async def get_attachment(message_id: uuid.UUID, attachment_id: uuid.UUID) -> Response:
    """Stream an attachment's bytes with its content type and a download disposition."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Attachment).where(
                Attachment.id == attachment_id, Attachment.message_id == message_id,
            )
        )
        attachment = result.scalar_one_or_none()

    if attachment is None or attachment.data is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    content_type = attachment.content_type or "application/octet-stream"
    filename = attachment.filename or str(attachment_id)
    return Response(
        content=attachment.data,
        media_type=content_type,
        headers={"Content-Disposition": content_disposition(filename)},
    )


@router.get("/{message_id}/raw")
async def get_raw_source(message_id: uuid.UUID) -> Response:
    """
    Download a message's full RFC822 source as a .eml file.

    raw_source is the entire message on every row, so this is its own
    single-column query rather than reusing MessageDetail -- and NULL when
    is_truncated, since a message over storage.max_message_bytes was never
    fetched from IMAP at all.
    """
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Message.subject, Message.raw_source, Message.is_truncated)
            .where(Message.id == message_id)
        )
        row = result.one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if row.raw_source is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "No raw source stored for this message -- it exceeded "
                "storage.max_message_bytes when it was fetched and was "
                "never downloaded from IMAP."
                if row.is_truncated
                else "No raw source stored for this message."
            ),
        )

    filename = f"{row.subject or 'message'}.eml"
    return Response(
        content=row.raw_source,
        media_type="message/rfc822",
        headers={"Content-Disposition": content_disposition(filename)},
    )


def _text_to_html(text: str) -> str:
    """Escape plain text and join its lines with <br>, for quoting a
    message that never had an HTML part at all."""
    return "<br>".join(html.escape(line) for line in text.splitlines())


@router.get("/{message_id}/quote", response_model=MessageQuoteResponse)
async def get_message_quote(message_id: uuid.UUID) -> MessageQuoteResponse:
    """
    A message's body as HTML, shaped for local display, for embedding as a
    reply or forward quote in the compose editor.

    Reads the raw body_html column rather than the display-shaped one
    get_message returns: that copy has cid: images rewritten to local,
    unauthenticated attachment URLs, which means nothing to a message
    actually being sent. Starting from the raw column and running it
    through the same outbound sanitiser every other producer of
    outbox.body_html goes through keeps that mapping in one place -- a
    remote image quotes as the sender's own absolute URL, a cid: or
    locally-rewritten one simply disappears, since there is nothing to
    attach it to.

    That real-URL form is then rewritten to the same data-x-src/data-x-bg
    placeholder the reading pane uses, and restored only if this message's
    own sender is already allowlisted -- the read path's own rule, applied
    here too because the editor's quote node renders this HTML locally
    (assigns it to innerHTML), where an unrewritten remote image would
    fetch on every reply, reply-all, forward and reopened draft regardless
    of whether the sender has ever been allowed to. Whatever placeholder
    survives that is restored again, unconditionally, by create_outbox()
    before a message actually leaves -- an allowlist decision about what
    loads automatically in this reader is not a decision about what the
    person being forwarded to gets to see.
    """
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(
                Message.body_html, Message.body_text,
                Message.account_id, Message.from_addr,
            ).where(
                Message.id == message_id, Message.expunged_at.is_(None),
            )
        )
        row = result.one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail="Message not found")

    body_html, body_text, account_id, from_addr = row
    if body_html:
        # restore_remote_images is a no-op unless body_html already carries
        # a data-x-src/data-x-style marker -- which it never should, since
        # create_outbox() restores before anything is stored -- but is
        # cheap defensive normalisation before the outbound sanitiser,
        # which has no allowlist entry for either marker and would drop
        # the image outright rather than pass it through unrecognised.
        sanitized = sanitize_outbound_html(restore_remote_images(body_html))
        display_html = rewrite_remote_images(sanitized)
        if await is_sender_image_allowed(account_id, from_addr):
            display_html = restore_remote_images(display_html)
        return MessageQuoteResponse(html=display_html)
    if body_text:
        return MessageQuoteResponse(html=f"<p>{_text_to_html(body_text)}</p>")
    return MessageQuoteResponse(html="")


@router.post("/{message_id}/action", response_model=MessageActionResponse)
async def message_action(
    message_id: uuid.UUID,
    request: MessageActionRequest,
) -> MessageActionResponse:
    """
    Perform an action on a message.

    Updates the local DB immediately. PostIMAP's PG trigger propagates
    changes to IMAP; postimap/listener.py fans the resulting event out
    to SSE, so this handler never emits one itself.
    """
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Message).where(Message.id == message_id))
        msg = result.scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")

    action = request.action
    account_id = msg.account_id

    if action in ("mark_read", "mark_unread"):
        async with db.session() as session:
            await set_flags(session, message_id, is_seen=(action == "mark_read"))
        return MessageActionResponse(success=True, action=action, message_id=message_id)

    if action in ("flag", "unflag"):
        async with db.session() as session:
            await set_flags(session, message_id, is_flagged=(action == "flag"))
        return MessageActionResponse(success=True, action=action, message_id=message_id)

    if action == "keyword_add" or action == "keyword_remove":
        if not request.keyword:
            raise HTTPException(status_code=400, detail=f"keyword required for {action}")
        current = set(msg.keywords or [])
        current = current | {request.keyword} if action == "keyword_add" else current - {
            request.keyword,
        }
        async with db.session() as session:
            await set_keywords(session, message_id, sorted(current))
        return MessageActionResponse(success=True, action=action, message_id=message_id)

    if action == "trash":
        trash_folder_id = await _resolve_special_folder(account_id, "trash")
        if trash_folder_id is None:
            raise HTTPException(status_code=400, detail="No trash folder found for this account")
        async with db.session() as session:
            await move_to_trash(session, message_id, trash_folder_id)
        return MessageActionResponse(
            success=True, action=action, message_id=message_id, message="Moved to trash",
        )

    if action == "expunge":
        async with db.session() as session:
            await expunge(session, message_id)
        return MessageActionResponse(
            success=True, action=action, message_id=message_id, message="Permanently deleted",
        )

    if action == "move":
        if not request.target_folder_id:
            raise HTTPException(status_code=400, detail="target_folder_id required for move")
        async with db.session() as session:
            if not await _folder_belongs_to_account(session, account_id, request.target_folder_id):
                raise HTTPException(
                    status_code=400, detail="target_folder_id does not belong to this account",
                )
            await move_message(session, message_id, request.target_folder_id)
        return MessageActionResponse(success=True, action=action, message_id=message_id)

    if action == "archive":
        target_folder_id = await _resolve_special_folder(account_id, "archive")
        if target_folder_id is None:
            raise HTTPException(status_code=400, detail="No archive folder found for this account")
        async with db.session() as session:
            await move_message(session, message_id, target_folder_id)
        return MessageActionResponse(success=True, action=action, message_id=message_id)

    if action in ("spam", "not_spam"):
        return await _handle_spam_action(db, message_id, account_id, is_spam=action == "spam")

    raise HTTPException(status_code=400, detail=f"Unknown action: {action}")


async def _handle_spam_action(
    db: DatabaseConnection, message_id: uuid.UUID, account_id: uuid.UUID, *, is_spam: bool,
) -> MessageActionResponse:
    """
    Move a message to/from junk and record the user's correction.

    The 'spam' direction is also caught by the automatic postimap_events
    listener (a move into a junk-special-use folder always records
    feedback), so only the 'not_spam' direction needs an explicit record
    here to avoid a duplicate verdict row for the same click.
    """
    role = "junk" if is_spam else "inbox"
    target_folder_id = await _resolve_special_folder(account_id, role)
    if target_folder_id is None:
        raise HTTPException(status_code=400, detail=f"No {role} folder found for this account")

    async with db.session() as session:
        await move_message(session, message_id, target_folder_id)

    if not is_spam:
        from mail_verdict.server import get_spam_processor

        processor = get_spam_processor()
        if processor is not None:
            await processor.feedback.handle_moved_from_spam(message_id, account_id)

    action = "spam" if is_spam else "not_spam"
    return MessageActionResponse(
        success=True, action=action, message_id=message_id,
        message="Marked as spam" if is_spam else "Marked as not spam",
    )


async def _resolve_special_folder(account_id: uuid.UUID, role: str) -> uuid.UUID | None:
    """
    Resolve a special folder UUID by its effective special_use.

    folder_prefs.special_use_override exists for servers that don't
    advertise SPECIAL-USE -- matching only the raw Folder.special_use (as
    list_folders does not) means every trash/archive/spam action fails with
    "no trash folder found" on exactly the servers the override is for.

    Args:
        account_id: Account to look up
        role: Folder role key (e.g., "archive", "junk", "trash", "inbox")

    Returns:
        Folder UUID or None if not found
    """
    from mail_verdict.database.repository import FolderRepository

    return await FolderRepository(get_db_connection()).resolve_special_folder(account_id, role)


async def _folder_belongs_to_account(
    session: AsyncSession, account_id: uuid.UUID, folder_id: uuid.UUID,
) -> bool:
    """
    A move's target folder is client-supplied and never otherwise checked
    against the message's own account -- without this, a client can move
    a message into another account's folder, since move_message() and
    move_message_bulk() write folder_id with no ownership check of their
    own (they trust the caller, same as every other postimap/actions.py
    helper).

    Args:
        session: Active AsyncSession
        account_id: The account the message being moved belongs to
        folder_id: The client-supplied target folder

    Returns:
        True if `folder_id` exists and belongs to `account_id`
    """
    result = await session.execute(
        select(Folder.id).where(Folder.id == folder_id, Folder.account_id == account_id)
    )
    return result.scalar_one_or_none() is not None


@account_router.get("/selection", response_model=SelectionSnapshotResponse)
async def mint_selection(
    account_id: uuid.UUID,
    folder_id: uuid.UUID = Query(...),
    filter: Literal["unread", "all"] = Query(default="all"),  # noqa: A002
) -> SelectionSnapshotResponse:
    """
    Mint a 'select all matching' snapshot: the current instant and the
    predicate's count at that instant, from one statement so the two can
    never disagree. Side-effect free -- no selection state is created here,
    the client holds the returned descriptor and sends it back on the
    bulk-action request that acts on it.
    """
    db = get_db_connection()
    async with db.session() as session:
        stmt = select(func.now(), func.count(Message.id)).where(
            Message.account_id == account_id,
            Message.folder_id == folder_id,
            Message.expunged_at.is_(None),
        )
        if filter == "unread":
            stmt = stmt.where(Message.is_seen.is_(False))
        row = (await session.execute(stmt)).one()
    return SelectionSnapshotResponse(snapshot_at=row[0], count=row[1])


@account_router.post("/bulk-action", response_model=BulkActionResponse)
async def bulk_action(account_id: uuid.UUID, request: BulkActionRequest) -> BulkActionResponse:
    """
    Apply one action to many messages, selected by an id list, a scope, or
    both -- a predicate scope plus explicit ids added on top of it (a row
    outside the predicate the user ticked by hand).

    A scope resolves server-side ("everything unread in this folder") so a
    virtualized, never-fully-fetched list can still "select all" without
    the client holding every id.
    """
    db = get_db_connection()

    async with db.session() as session:
        resolved: set[uuid.UUID] = set()
        if request.scope is not None:
            resolved.update(await _resolve_scope_ids(session, account_id, request.scope))
        if request.ids:
            # An explicit id list is client-supplied and otherwise never
            # checked against the path's account_id -- narrowed to the
            # ids that actually belong here (and still exist) the same
            # way a scope already is, rather than trusting the list.
            resolved.update(await _resolve_explicit_ids(session, account_id, request.ids))
        message_ids = list(resolved)

    # A caller that showed a count to a user before sending this request
    # (an "empty this folder" confirmation, most concretely) repeats it
    # back here -- checked against what actually resolves now, not what
    # was true when it was minted. Mirrors folder deletion's own
    # confirm_message_count gate: a stale or optimistically-adjusted count
    # must not be able to make an irreversible write look confirmed when
    # it wasn't. Most actions pass nothing and skip this entirely.
    confirmed = request.confirm_message_count
    if confirmed is not None and confirmed != len(message_ids):
        raise HTTPException(
            status_code=409,
            detail=(
                f"This resolves to {len(message_ids)} message(s) now, not the "
                f"{confirmed} confirmed. Repeat the request "
                f"with confirm_message_count={len(message_ids)} to proceed."
            ),
        )

    if not message_ids:
        return BulkActionResponse(success=True, action=request.action, affected_count=0)

    action = request.action
    errors: list[str] = []
    affected = 0

    if action in ("mark_read", "mark_unread"):
        async with db.session() as session:
            affected = await set_flags_bulk(session, message_ids, is_seen=(action == "mark_read"))
    elif action in ("flag", "unflag"):
        async with db.session() as session:
            affected = await set_flags_bulk(
                session, message_ids, is_flagged=(action == "flag"),
            )
    elif action == "trash":
        trash_folder_id = await _resolve_special_folder(account_id, "trash")
        if trash_folder_id is None:
            errors.append("No trash folder found for this account")
        else:
            async with db.session() as session:
                affected = await move_message_bulk(session, message_ids, trash_folder_id)
    elif action == "expunge":
        async with db.session() as session:
            affected = await expunge_bulk(session, message_ids)
    elif action == "move":
        if not request.target_folder_id:
            raise HTTPException(status_code=400, detail="target_folder_id required for move")
        async with db.session() as session:
            if not await _folder_belongs_to_account(session, account_id, request.target_folder_id):
                raise HTTPException(
                    status_code=400, detail="target_folder_id does not belong to this account",
                )
            affected = await move_message_bulk(session, message_ids, request.target_folder_id)
    elif action in ("archive", "spam", "not_spam"):
        role = {"archive": "archive", "spam": "junk", "not_spam": "inbox"}[action]
        folder_id = await _resolve_special_folder(account_id, role)
        if folder_id is None:
            errors.append(f"No {role} folder found for this account")
        else:
            async with db.session() as session:
                affected = await move_message_bulk(session, message_ids, folder_id)
            if action == "not_spam":
                from mail_verdict.server import get_spam_processor

                processor = get_spam_processor()
                if processor is not None:
                    for mid in message_ids:
                        await processor.feedback.handle_moved_from_spam(mid, account_id)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    return BulkActionResponse(
        success=not errors, action=action, affected_count=affected, errors=errors,
    )


async def _resolve_explicit_ids(
    session: AsyncSession, account_id: uuid.UUID, ids: list[uuid.UUID],
) -> list[uuid.UUID]:
    """
    Narrow a client-supplied id list to the ones that actually belong to
    this account and still exist.

    Without this, a bulk action's explicit-id path (unlike its scope
    path, which is already account-scoped by construction) acts on
    whatever ids a client names -- including one belonging to a different
    account, or one already expunged. An id that fails either check is
    silently dropped rather than acted on; affected_count then reflects
    the resolved subset, not the length of what was asked for.

    Matched with `= ANY(:ids)` rather than `IN (...)`: an IN clause binds
    one parameter per id, and asyncpg refuses a statement over 32767
    parameters -- exactly what a large "select all in this folder" client
    can send. ANY binds the whole list as a single array parameter.
    """
    if not ids:
        return []
    result = await session.execute(
        select(Message.id).where(
            Message.id == any_(ids), Message.account_id == account_id,  # type: ignore[arg-type]
            Message.expunged_at.is_(None),
        )
    )
    return list(result.scalars().all())


async def _resolve_scope_ids(
    session: AsyncSession, account_id: uuid.UUID, scope: BulkActionScope,
) -> list[uuid.UUID]:
    """
    Resolve a bulk-action scope descriptor to a concrete list of message ids.

    `created_at <= snapshot_at` excludes anything mirrored after the client
    minted this scope -- the guard against sweeping in mail that arrived
    between "select all" and the button press, which the user never agreed
    to and never saw. exclude_ids is matched with `!= ALL(:ids)` rather
    than `NOT IN (...)` for the same reason as _resolve_explicit_ids's
    `= ANY(:ids)`.
    """
    stmt = select(Message.id).where(
        Message.account_id == account_id,
        Message.folder_id == scope.folder_id,
        Message.expunged_at.is_(None),
        Message.created_at <= scope.snapshot_at,
    )
    if scope.filter == "unread":
        stmt = stmt.where(Message.is_seen.is_(False))
    if scope.exclude_ids:
        stmt = stmt.where(Message.id != all_(scope.exclude_ids))  # type: ignore[arg-type]
    result = await session.execute(stmt)
    return list(result.scalars().all())
