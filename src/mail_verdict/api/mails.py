"""
Message API endpoints.

GET /api/mails — cursor-based paginated list with filters
GET /api/mails/:id — detail view
POST /api/mails/:id/action — actions (move, mark, delete)

PostIMAP integration: SQL UPDATEs are sufficient for all actions.
PostIMAP's PG trigger handles IMAP propagation; MailVerdict's
mv_message_notify trigger handles SSE events.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import and_, desc, or_, select, update

from mail_verdict.api.deps import (
    get_attachment_repo,
    get_folder_repo,
    get_message_repo,
    get_tag_repo,
)
from mail_verdict.api.schemas import (
    AttachmentSummary,
    MessageActionRequest,
    MessageActionResponse,
    MessageDetail,
    MessageListResponse,
    MessageSummary,
    TagResponse,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Folder, Message

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mails", tags=["mails"])


@router.get("", response_model=MessageListResponse)
async def list_messages(
    account_id: uuid.UUID = Query(),
    folder_id: uuid.UUID | None = Query(default=None),
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

    Requires account_id to scope results to a single account.
    Cursor pagination uses the `before` parameter (UUID of the last message
    in the previous page). Stable under concurrent inserts.
    First page: omit `before`. Subsequent pages: pass `next_cursor` from response.
    """
    db = get_db_connection()
    async with db.session() as session:
        stmt = (
            select(Message)
            .where(Message.deleted_at.is_(None))
            .order_by(desc(Message.received_at), desc(Message.id))
        )

        stmt = stmt.where(Message.account_id == account_id)
        if folder_id is not None:
            stmt = stmt.where(Message.folder_id == folder_id)
        if is_seen is not None:
            stmt = stmt.where(Message.is_seen == is_seen)
        if since is not None:
            stmt = stmt.where(Message.received_at >= since)

        # Cursor-based pagination: fetch messages older than the cursor
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
            # Tiebreaker: messages with same received_at sorted by id desc
            stmt = stmt.where(
                or_(
                    Message.received_at < cursor_received_at,
                    and_(
                        Message.received_at == cursor_received_at,
                        Message.id < cursor_id,
                    ),
                )
            )

        # Fetch one extra to determine has_more
        stmt = stmt.limit(limit + 1)
        result = await session.execute(stmt)
        messages = list(result.scalars().all())

    has_more = len(messages) > limit
    if has_more:
        messages = messages[:limit]

    next_cursor = str(messages[-1].id) if has_more and messages else None

    return MessageListResponse(
        messages=[
            MessageSummary(
                **{
                    k: v
                    for k, v in MessageSummary.model_validate(m).model_dump().items()
                    if k != "snippet"
                },
                snippet=m.body_text[:120] if m.body_text else None,
            )
            for m in messages
        ],
        has_more=has_more,
        next_cursor=next_cursor,
    )


@router.get("/{message_id}", response_model=MessageDetail)
async def get_message(
    message_id: uuid.UUID,
    account_id: uuid.UUID = Query(),
    load_images: bool = Query(default=False, description="Load remote images if allowed"),
) -> MessageDetail:
    """
    Get full message detail by ID.

    Returns the message with attachments, tags, and image privacy controls.
    """
    msg_repo = get_message_repo()
    msg = await msg_repo.get_by_id(account_id, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")

    tag_repo = get_tag_repo()
    tags = await tag_repo.get_tags_for_mail(message_id)

    attachment_repo = get_attachment_repo()
    attachments = await attachment_repo.get_by_message_id(message_id)

    from mail_verdict.core.image_sanitizer import (
        restore_remote_images,
        strip_remote_images,
    )

    # HTML is already nh3-sanitized at store time.
    # Read-time processing only handles remote image blocking/restoration.
    body_html = msg.body_html

    images_allowed = False
    has_blocked_images = False
    if body_html:
        images_allowed = await _check_image_allowed(
            account_id, msg.from_addr,
        )

        if images_allowed and load_images:
            body_html = restore_remote_images(body_html)
            has_blocked_images = False
        else:
            body_html, has_blocked_images = strip_remote_images(body_html)

    return MessageDetail(
        id=msg.id,
        account_id=msg.account_id,
        folder_id=msg.folder_id,
        imap_uid=msg.imap_uid,
        message_id=msg.message_id,
        subject=msg.subject,
        from_addr=msg.from_addr,
        to_addrs=msg.to_addrs,
        cc_addrs=msg.cc_addrs,
        bcc_addrs=msg.bcc_addrs,
        reply_to=msg.reply_to,
        in_reply_to=msg.in_reply_to,
        body_text=msg.body_text,
        body_html=body_html,
        raw_headers=msg.raw_headers,
        received_at=msg.received_at,
        size_bytes=msg.size_bytes,
        is_seen=msg.is_seen,
        is_flagged=msg.is_flagged,
        is_answered=msg.is_answered,
        is_draft=msg.is_draft,
        is_deleted=msg.is_deleted,
        keywords=msg.keywords or [],
        deleted_at=msg.deleted_at,
        created_at=msg.created_at,
        has_blocked_images=has_blocked_images,
        images_allowed=images_allowed,
        tags=[TagResponse(tag_name=t.tag_name, source=t.source.value) for t in tags],
        attachments=[
            AttachmentSummary(
                id=a.id,
                filename=a.filename,
                content_type=a.content_type,
                size_bytes=a.size_bytes,
            )
            for a in attachments
        ],
    )


async def _check_image_allowed(
    account_id: uuid.UUID,
    from_addr: str | None,
) -> bool:
    """
    Check if images are allowed for a sender based on image exceptions.

    Args:
        account_id: Account to check exceptions for
        from_addr: Sender email address

    Returns:
        True if sender or domain is in the exception allowlist
    """
    from mail_verdict.core.image_sanitizer import extract_sender_domain, extract_sender_email
    from mail_verdict.database.models import ImageException, ImageExceptionType

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
            .where(
                ImageException.account_id == account_id,
                or_(*conditions),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


@router.post("/{message_id}/action", response_model=MessageActionResponse)
async def message_action(
    message_id: uuid.UUID,
    request: MessageActionRequest,
    account_id: uuid.UUID = Query(),
) -> MessageActionResponse:
    """
    Perform an action on a message.

    Supported actions: move, mark_read, mark_unread, delete, flag, unflag,
    archive, spam.

    Updates the local DB immediately. PostIMAP's PG trigger propagates
    changes to IMAP. MailVerdict's mv_message_notify trigger pushes SSE events.
    """
    msg_repo = get_message_repo()
    msg = await msg_repo.get_by_id(account_id, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")

    db = get_db_connection()
    action = request.action
    source_folder_id = msg.folder_id

    if action == "mark_read":
        async with db.session() as session:
            await session.execute(
                update(Message).where(Message.id == message_id).values(is_seen=True)
            )
        await _emit_message_updated_event(
            account_id, source_folder_id, message_id, msg.imap_uid,
            is_seen=True, is_flagged=msg.is_flagged,
        )
        return MessageActionResponse(success=True, action=action, message_id=message_id)

    elif action == "mark_unread":
        async with db.session() as session:
            await session.execute(
                update(Message).where(Message.id == message_id).values(is_seen=False)
            )
        await _emit_message_updated_event(
            account_id, source_folder_id, message_id, msg.imap_uid,
            is_seen=False, is_flagged=msg.is_flagged,
        )
        return MessageActionResponse(success=True, action=action, message_id=message_id)

    elif action == "flag":
        async with db.session() as session:
            await session.execute(
                update(Message).where(Message.id == message_id).values(is_flagged=True)
            )
        await _emit_message_updated_event(
            account_id, source_folder_id, message_id, msg.imap_uid,
            is_seen=msg.is_seen, is_flagged=True,
        )
        return MessageActionResponse(success=True, action=action, message_id=message_id)

    elif action == "unflag":
        async with db.session() as session:
            await session.execute(
                update(Message).where(Message.id == message_id).values(is_flagged=False)
            )
        await _emit_message_updated_event(
            account_id, source_folder_id, message_id, msg.imap_uid,
            is_seen=msg.is_seen, is_flagged=False,
        )
        return MessageActionResponse(success=True, action=action, message_id=message_id)

    elif action == "delete":
        # Move to trash if mapped and not already in trash; otherwise permanent delete
        trash_folder_id = await _resolve_special_folder(account_id, "trash")
        if trash_folder_id and source_folder_id != trash_folder_id:
            # Move to trash
            async with db.session() as session:
                await session.execute(
                    update(Message).where(Message.id == message_id)
                    .values(folder_id=trash_folder_id)
                )
            await _emit_message_deleted_event(
                account_id, source_folder_id, message_id, msg.imap_uid,
            )
            return MessageActionResponse(
                success=True, action=action, message_id=message_id,
                message="Moved to trash",
            )
        else:
            # Permanent delete (already in trash or no trash folder)
            now = datetime.now(timezone.utc)
            async with db.session() as session:
                await session.execute(
                    update(Message).where(Message.id == message_id).values(deleted_at=now)
                )
            await _emit_message_deleted_event(
                account_id, source_folder_id, message_id, msg.imap_uid,
            )
            return MessageActionResponse(
                success=True, action=action, message_id=message_id,
                message="Permanently deleted",
            )

    elif action == "move":
        if not request.target_folder:
            raise HTTPException(
                status_code=400,
                detail="target_folder required for move action",
            )

        folder_repo = get_folder_repo()
        folders = await folder_repo.get_by_account(account_id)
        target = next((f for f in folders if f.imap_name == request.target_folder), None)

        if target is None:
            raise HTTPException(
                status_code=400,
                detail=f"Folder not found: {request.target_folder}",
            )

        async with db.session() as session:
            await session.execute(
                update(Message).where(Message.id == message_id)
                .values(folder_id=target.id)
            )
        await _emit_message_deleted_event(
            account_id, source_folder_id, message_id, msg.imap_uid,
        )
        return MessageActionResponse(
            success=True,
            action=action,
            message_id=message_id,
            message=f"Moved to {target.imap_name}",
        )

    elif action in ("archive", "spam"):
        target_folder_id = await _resolve_special_folder(
            account_id, "archive" if action == "archive" else "junk",
        )
        if target_folder_id is None:
            raise HTTPException(
                status_code=400,
                detail=f"No {action} folder mapped for this account",
            )

        async with db.session() as session:
            await session.execute(
                update(Message).where(Message.id == message_id)
                .values(folder_id=target_folder_id)
            )

        await _emit_message_deleted_event(
            account_id, source_folder_id, message_id, msg.imap_uid,
        )
        return MessageActionResponse(
            success=True,
            action=action,
            message_id=message_id,
            message=f"{'Archived' if action == 'archive' else 'Marked as spam'}",
        )

    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")


async def _resolve_special_folder(
    account_id: uuid.UUID,
    role: str,
) -> uuid.UUID | None:
    """
    Resolve a special folder UUID by querying Folder.special_use directly.

    Args:
        account_id: Account to look up
        role: Folder role key (e.g., "archive", "junk", "trash")

    Returns:
        Folder UUID or None if not found
    """
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Folder.id).where(
                Folder.account_id == account_id,
                Folder.special_use == role,
            ).limit(1)
        )
        return result.scalar_one_or_none()


async def _emit_message_updated_event(
    account_id: uuid.UUID,
    folder_id: uuid.UUID,
    message_id: uuid.UUID,
    imap_uid: int,
    is_seen: bool,
    is_flagged: bool,
) -> None:
    """
    Emit a mail.updated SSE event after an action.

    Args:
        account_id: Account UUID
        folder_id: Folder UUID (current or target)
        message_id: Message UUID
        imap_uid: IMAP UID
        is_seen: Current seen state
        is_flagged: Current flagged state
    """
    from mail_verdict.api.events import get_event_ring

    ring = get_event_ring()
    if ring is None:
        return

    await ring.add(
        account_id=account_id,
        event_type="mail.updated",
        data={
            "account_id": str(account_id),
            "folder_id": str(folder_id),
            "message_id": str(message_id),
            "imap_uid": imap_uid,
            "is_seen": is_seen,
            "is_flagged": is_flagged,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


async def _emit_message_deleted_event(
    account_id: uuid.UUID,
    folder_id: uuid.UUID,
    message_id: uuid.UUID,
    imap_uid: int,
) -> None:
    """
    Emit a mail.deleted SSE event after a delete/move action.

    Args:
        account_id: Account UUID
        folder_id: Folder UUID
        message_id: Message UUID
        imap_uid: IMAP UID
    """
    from mail_verdict.api.events import get_event_ring

    ring = get_event_ring()
    if ring is None:
        return

    await ring.add(
        account_id=account_id,
        event_type="mail.deleted",
        data={
            "account_id": str(account_id),
            "folder_id": str(folder_id),
            "message_id": str(message_id),
            "imap_uid": imap_uid,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
