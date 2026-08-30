"""
Message API endpoints.

GET /api/accounts/:account_id/messages — cursor-paginated list, optionally
  grouped into one row per conversation (threaded=true)
GET /api/messages/:id — detail view (sanitized HTML, embedded verdict)
GET /api/messages/:id/thread — every message in the conversation, ascending
GET /api/messages/:id/attachments/:attachment_id — streamed attachment
POST /api/messages/:id/action — single-message action
POST /api/accounts/:account_id/messages/bulk-action — action over many
  messages, by id list or by a server-resolved scope

PostIMAP integration: SQL UPDATEs are sufficient for all actions --
postimap/actions.py owns the write shapes; PostIMAP's own triggers
propagate them to IMAP, and postimap/listener.py fans them out to SSE.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import and_, case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from mail_verdict.api.deps import (
    get_attachment_repo,
    get_tag_repo,
    get_verdict_repo,
)
from mail_verdict.api.schemas import (
    AttachmentSummary,
    BulkActionRequest,
    BulkActionResponse,
    BulkActionScope,
    MessageActionRequest,
    MessageActionResponse,
    MessageDetail,
    MessageListResponse,
    MessageSummary,
    TagResponse,
    ThreadResponse,
    VerdictResponse,
)
from mail_verdict.core.image_sanitizer import (
    extract_sender_domain,
    extract_sender_email,
    restore_remote_images,
    strip_remote_images,
)
from mail_verdict.core.sanitizer import sanitize_email_html
from mail_verdict.database.connection import DatabaseConnection, get_db_connection
from mail_verdict.database.models import (
    Attachment,
    ImageException,
    ImageExceptionType,
    Message,
)
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


@account_router.get("", response_model=MessageListResponse)
async def list_messages(
    account_id: uuid.UUID,
    folder_id: uuid.UUID | None = Query(default=None),
    threaded: bool = Query(default=False, description="One row per conversation"),
    is_seen: bool | None = Query(default=None),
    since: datetime | None = Query(default=None),
    before: uuid.UUID | None = Query(
        default=None,
        description="Cursor: UUID of last message in previous page",
    ),
    limit: int = Query(default=50, ge=1, le=200),
) -> MessageListResponse:
    """
    List messages with cursor-based pagination.

    threaded=true groups by thread_id: one row per conversation (its latest
    message, plus thread_count/unread_in_thread scoped to this same folder
    filter), ordered by that latest message's received_at.
    Cursor pagination uses the `before` parameter (UUID of the last message
    in the previous page). Stable under concurrent inserts.
    """
    db = get_db_connection()
    async with db.session() as session:
        cursor_received_at, cursor_id = None, None
        if before is not None:
            cursor_result = await session.execute(
                select(Message.received_at, Message.id).where(Message.id == before)
            )
            cursor_row = cursor_result.one_or_none()
            if cursor_row is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid cursor: message {before} not found",
                )
            cursor_received_at, cursor_id = cursor_row

        if threaded:
            rows = await _list_messages_threaded(
                session, account_id, folder_id, is_seen, since,
                cursor_received_at, cursor_id, limit,
            )
            messages = [
                MessageSummary(
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
                )
                for m, thread_count, unread_in_thread in rows[:limit]
            ]
            has_more = len(rows) > limit
        else:
            stmt = (
                select(Message)
                .where(Message.expunged_at.is_(None), Message.account_id == account_id)
                .order_by(desc(Message.received_at), desc(Message.id))
            )
            if folder_id is not None:
                stmt = stmt.where(Message.folder_id == folder_id)
            if is_seen is not None:
                stmt = stmt.where(Message.is_seen == is_seen)
            if since is not None:
                stmt = stmt.where(Message.received_at >= since)
            if cursor_received_at is not None:
                stmt = stmt.where(
                    or_(
                        Message.received_at < cursor_received_at,
                        and_(
                            Message.received_at == cursor_received_at,
                            Message.id < cursor_id,
                        ),
                    )
                )
            stmt = stmt.limit(limit + 1)
            result = await session.execute(stmt)
            all_msgs = list(result.scalars().all())
            has_more = len(all_msgs) > limit
            messages = [
                MessageSummary(
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
                )
                for m in all_msgs[:limit]
            ]

    next_cursor = str(messages[-1].id) if has_more and messages else None
    return MessageListResponse(messages=messages, has_more=has_more, next_cursor=next_cursor)


async def _list_messages_threaded(
    session: AsyncSession,
    account_id: uuid.UUID,
    folder_id: uuid.UUID | None,
    is_seen: bool | None,
    since: datetime | None,
    cursor_received_at: datetime | None,
    cursor_id: uuid.UUID | None,
    limit: int,
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
        .order_by(desc(latest.received_at), desc(latest.id))
    )
    if cursor_received_at is not None:
        stmt = stmt.where(
            or_(
                latest.received_at < cursor_received_at,
                and_(latest.received_at == cursor_received_at, latest.id < cursor_id),
            )
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
        result = await session.execute(select(Message).where(Message.id == message_id))
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

        images_allowed = await _check_image_allowed(msg.account_id, msg.from_addr)

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
                images_allowed = await _check_image_allowed(m.account_id, m.from_addr)
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
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _check_image_allowed(account_id: uuid.UUID, from_addr: str | None) -> bool:
    """
    Check if images are allowed for a sender based on image exceptions.

    Args:
        account_id: Account to check exceptions for
        from_addr: Sender email address

    Returns:
        True if sender or domain is in the exception allowlist
    """
    if not from_addr:
        return False

    email = extract_sender_email(from_addr)
    domain = extract_sender_domain(from_addr)

    db = get_db_connection()
    async with db.session() as session:
        conditions = []
        if email:
            conditions.append(
                (ImageException.exception_type == ImageExceptionType.SENDER)
                & (ImageException.value == email)
            )
        if domain:
            conditions.append(
                (ImageException.exception_type == ImageExceptionType.DOMAIN)
                & (ImageException.value == domain)
            )
        if not conditions:
            return False

        result = await session.execute(
            select(ImageException.id)
            .where(ImageException.account_id == account_id, or_(*conditions))
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


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
        if processor is not None and processor._feedback is not None:
            await processor._feedback.handle_moved_from_spam(message_id, account_id)

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


@account_router.post("/bulk-action", response_model=BulkActionResponse)
async def bulk_action(account_id: uuid.UUID, request: BulkActionRequest) -> BulkActionResponse:
    """
    Apply one action to many messages, selected by id list or by scope.

    A scope resolves server-side ("everything unread in this folder") so a
    virtualized, never-fully-fetched list can still "select all" without
    the client holding every id.
    """
    db = get_db_connection()
    target = request.resolved_ids_or_scope()

    async with db.session() as session:
        if isinstance(target, list):
            message_ids = target
        else:
            message_ids = await _resolve_scope_ids(session, account_id, target)

    if not message_ids:
        return BulkActionResponse(success=True, action=request.action, affected_count=0)

    action = request.action
    errors: list[str] = []

    if action in ("mark_read", "mark_unread"):
        async with db.session() as session:
            await set_flags_bulk(session, message_ids, is_seen=(action == "mark_read"))
    elif action in ("flag", "unflag"):
        async with db.session() as session:
            await set_flags_bulk(session, message_ids, is_flagged=(action == "flag"))
    elif action == "trash":
        trash_folder_id = await _resolve_special_folder(account_id, "trash")
        if trash_folder_id is None:
            errors.append("No trash folder found for this account")
        else:
            async with db.session() as session:
                await move_message_bulk(session, message_ids, trash_folder_id)
    elif action == "expunge":
        async with db.session() as session:
            await expunge_bulk(session, message_ids)
    elif action == "move":
        if not request.target_folder_id:
            raise HTTPException(status_code=400, detail="target_folder_id required for move")
        async with db.session() as session:
            await move_message_bulk(session, message_ids, request.target_folder_id)
    elif action in ("archive", "spam", "not_spam"):
        role = {"archive": "archive", "spam": "junk", "not_spam": "inbox"}[action]
        folder_id = await _resolve_special_folder(account_id, role)
        if folder_id is None:
            errors.append(f"No {role} folder found for this account")
        else:
            async with db.session() as session:
                await move_message_bulk(session, message_ids, folder_id)
            if action == "not_spam":
                from mail_verdict.server import get_spam_processor

                processor = get_spam_processor()
                if processor is not None and processor._feedback is not None:
                    for mid in message_ids:
                        await processor._feedback.handle_moved_from_spam(mid, account_id)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    affected = 0 if errors else len(message_ids)
    return BulkActionResponse(
        success=not errors, action=action, affected_count=affected, errors=errors,
    )


async def _resolve_scope_ids(
    session: AsyncSession, account_id: uuid.UUID, scope: BulkActionScope,
) -> list[uuid.UUID]:
    """Resolve a bulk-action scope descriptor to a concrete list of message ids."""
    stmt = select(Message.id).where(
        Message.account_id == account_id,
        Message.folder_id == scope.folder_id,
        Message.expunged_at.is_(None),
    )
    if scope.filter == "unread":
        stmt = stmt.where(Message.is_seen.is_(False))
    if scope.exclude_ids:
        stmt = stmt.where(Message.id.notin_(scope.exclude_ids))
    result = await session.execute(stmt)
    return list(result.scalars().all())
