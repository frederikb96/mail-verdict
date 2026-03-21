"""
Mail API endpoints.

GET /api/mails — cursor-based paginated list with filters
GET /api/mails/:id — detail view with on-demand body fetch
POST /api/mails/:id/action — actions (move, mark, delete)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import desc, select

from mail_verdict.api.deps import (
    get_attachment_repo,
    get_folder_repo,
    get_mail_repo,
    get_tag_repo,
)
from mail_verdict.api.schemas import (
    AttachmentSummary,
    MailActionRequest,
    MailActionResponse,
    MailDetail,
    MailListResponse,
    MailSummary,
    TagResponse,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Mail

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mails", tags=["mails"])


@router.get("", response_model=MailListResponse)
async def list_mails(
    account_id: uuid.UUID | None = Query(default=None),
    folder_id: uuid.UUID | None = Query(default=None),
    is_read: bool | None = Query(default=None),
    since: datetime | None = Query(default=None),
    before: uuid.UUID | None = Query(
        default=None,
        description="Cursor: UUID of last mail in previous page",
    ),
    limit: int = Query(default=50, ge=1, le=200),
) -> MailListResponse:
    """
    List mails with cursor-based pagination.

    Cursor pagination uses the `before` parameter (UUID of the last mail
    in the previous page). Stable under concurrent inserts.
    First page: omit `before`. Subsequent pages: pass `next_cursor` from response.
    """
    db = get_db_connection()
    async with db.session() as session:
        stmt = (
            select(Mail)
            .where(Mail.is_deleted.is_(False))
            .order_by(desc(Mail.received_at), desc(Mail.id))
        )

        if account_id is not None:
            stmt = stmt.where(Mail.account_id == account_id)
        if folder_id is not None:
            stmt = stmt.where(Mail.folder_id == folder_id)
        if is_read is not None:
            stmt = stmt.where(Mail.is_read == is_read)
        if since is not None:
            stmt = stmt.where(Mail.received_at >= since)

        # Cursor-based pagination: fetch mails older than the cursor mail
        if before is not None:
            cursor_result = await session.execute(
                select(Mail.received_at, Mail.id).where(Mail.id == before)
            )
            cursor_row = cursor_result.one_or_none()
            if cursor_row is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid cursor: mail {before} not found",
                )
            cursor_received_at, cursor_id = cursor_row
            # Tiebreaker: mails with same received_at sorted by id desc
            from sqlalchemy import and_, or_

            stmt = stmt.where(
                or_(
                    Mail.received_at < cursor_received_at,
                    and_(
                        Mail.received_at == cursor_received_at,
                        Mail.id < cursor_id,
                    ),
                )
            )

        # Fetch one extra to determine has_more
        stmt = stmt.limit(limit + 1)
        result = await session.execute(stmt)
        mails = list(result.scalars().all())

    has_more = len(mails) > limit
    if has_more:
        mails = mails[:limit]

    next_cursor = str(mails[-1].id) if has_more and mails else None

    return MailListResponse(
        mails=[MailSummary.model_validate(m) for m in mails],
        has_more=has_more,
        next_cursor=next_cursor,
    )


@router.get("/{mail_id}", response_model=MailDetail)
async def get_mail(
    mail_id: uuid.UUID,
    account_id: uuid.UUID = Query(),
    load_images: bool = Query(default=False, description="Load remote images if allowed"),
) -> MailDetail:
    """
    Get full mail detail by ID.

    If the mail body has not been synced yet (body_synced=False),
    triggers an on-demand IMAP fetch before returning.
    Remote images are stripped unless sender/domain is in the exception list.
    """
    mail_repo = get_mail_repo()
    mail = await mail_repo.get_by_id(account_id, mail_id)
    if mail is None:
        raise HTTPException(status_code=404, detail="Mail not found")

    # On-demand body fetch if not yet synced
    if not mail.body_synced:
        await _fetch_body_on_demand(mail)
        # Re-fetch the mail to get updated body content
        mail = await mail_repo.get_by_id(account_id, mail_id)
        if mail is None:
            raise HTTPException(status_code=404, detail="Mail not found after body fetch")

    tag_repo = get_tag_repo()
    tags = await tag_repo.get_tags_for_mail(mail_id)

    attachment_repo = get_attachment_repo()
    attachments = await attachment_repo.get_by_mail_id(mail_id)

    from mail_verdict.core.image_sanitizer import (
        restore_remote_images,
        strip_remote_images,
    )
    from mail_verdict.core.sanitizer import sanitize_email_html

    sanitized_html = sanitize_email_html(mail.body_html) if mail.body_html else None

    # Check image exceptions for this sender
    images_allowed = False
    has_blocked_images = False
    if sanitized_html:
        images_allowed = await _check_image_allowed(
            account_id, mail.from_addr,
        )

        if images_allowed and load_images:
            sanitized_html = restore_remote_images(sanitized_html)
            has_blocked_images = False
        else:
            sanitized_html, has_blocked_images = strip_remote_images(sanitized_html)

    return MailDetail(
        id=mail.id,
        account_id=mail.account_id,
        folder_id=mail.folder_id,
        uid=mail.uid,
        message_id=mail.message_id,
        subject=mail.subject,
        from_addr=mail.from_addr,
        to_addrs=mail.to_addrs,
        cc_addrs=mail.cc_addrs,
        bcc_addrs=mail.bcc_addrs,
        body_text=mail.body_text,
        body_html=sanitized_html,
        raw_headers=mail.raw_headers,
        received_at=mail.received_at,
        size_bytes=mail.size_bytes,
        is_read=mail.is_read,
        is_flagged=mail.is_flagged,
        is_deleted=mail.is_deleted,
        dkim_pass=mail.dkim_pass,
        spf_pass=mail.spf_pass,
        dmarc_pass=mail.dmarc_pass,
        headers_synced=mail.headers_synced,
        body_synced=mail.body_synced,
        has_blocked_images=has_blocked_images,
        images_allowed=images_allowed,
        fetched_at=mail.fetched_at,
        created_at=mail.created_at,
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
        from sqlalchemy import or_

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


async def _fetch_body_on_demand(mail: Mail) -> None:
    """
    Trigger on-demand IMAP body fetch for a mail with body_synced=False.

    Looks up the sync engine's manager for the mail's account
    and fetches the body via the IMAP connector.

    Args:
        mail: Mail object with body_synced=False
    """
    from mail_verdict.server import get_sync_engine

    sync_engine = get_sync_engine()
    if sync_engine is None:
        logger.warning(
            "Sync engine not available for on-demand body fetch",
            extra={"mail_id": str(mail.id)},
        )
        return

    account_sync = sync_engine.get_account_sync_by_id(mail.account_id)
    if account_sync is None:
        logger.warning(
            "No active sync for account, cannot fetch body on-demand",
            extra={"mail_id": str(mail.id), "account_id": str(mail.account_id)},
        )
        return

    # Get folder IMAP name for the folder selection
    db = get_db_connection()
    async with db.session() as session:
        from sqlalchemy import select as sa_select

        from mail_verdict.database.models import Folder

        result = await session.execute(
            sa_select(Folder).where(Folder.id == mail.folder_id)
        )
        folder = result.scalar_one_or_none()

    if folder is None:
        logger.warning(
            "Folder not found for on-demand body fetch",
            extra={"mail_id": str(mail.id), "folder_id": str(mail.folder_id)},
        )
        return

    success = await account_sync.manager.fetch_body_for_mail(
        folder_imap_name=folder.imap_name,
        uid=mail.uid,
        folder_id=folder.id,
    )
    if not success:
        logger.warning(
            "On-demand body fetch did not succeed",
            extra={"mail_id": str(mail.id), "uid": mail.uid},
        )


@router.post("/{mail_id}/action", response_model=MailActionResponse)
async def mail_action(
    mail_id: uuid.UUID,
    request: MailActionRequest,
    account_id: uuid.UUID = Query(),
) -> MailActionResponse:
    """
    Perform an action on a mail.

    Supported actions: move, mark_read, mark_unread, delete, flag, unflag.
    """
    from sqlalchemy import update

    mail_repo = get_mail_repo()
    mail = await mail_repo.get_by_id(account_id, mail_id)
    if mail is None:
        raise HTTPException(status_code=404, detail="Mail not found")

    db = get_db_connection()
    action = request.action

    if action == "mark_read":
        async with db.session() as session:
            await session.execute(update(Mail).where(Mail.id == mail_id).values(is_read=True))
        return MailActionResponse(success=True, action=action, mail_id=mail_id)

    elif action == "mark_unread":
        async with db.session() as session:
            await session.execute(update(Mail).where(Mail.id == mail_id).values(is_read=False))
        return MailActionResponse(success=True, action=action, mail_id=mail_id)

    elif action == "flag":
        async with db.session() as session:
            await session.execute(update(Mail).where(Mail.id == mail_id).values(is_flagged=True))
        return MailActionResponse(success=True, action=action, mail_id=mail_id)

    elif action == "unflag":
        async with db.session() as session:
            await session.execute(update(Mail).where(Mail.id == mail_id).values(is_flagged=False))
        return MailActionResponse(success=True, action=action, mail_id=mail_id)

    elif action == "delete":
        async with db.session() as session:
            await session.execute(update(Mail).where(Mail.id == mail_id).values(is_deleted=True))
        return MailActionResponse(success=True, action=action, mail_id=mail_id)

    elif action == "move":
        if not request.target_folder:
            raise HTTPException(status_code=400, detail="target_folder required for move action")

        # Resolve target folder
        folder_repo = get_folder_repo()
        folders = await folder_repo.get_by_account(account_id)
        target = next(
            (f for f in folders if f.imap_name == request.target_folder),
            None,
        )
        if target is None:
            raise HTTPException(
                status_code=400,
                detail=f"Folder not found: {request.target_folder}",
            )

        async with db.session() as session:
            await session.execute(
                update(Mail).where(Mail.id == mail_id).values(folder_id=target.id)
            )
        return MailActionResponse(
            success=True,
            action=action,
            mail_id=mail_id,
            message=f"Moved to {request.target_folder}",
        )

    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")
