"""
Mail API endpoints.

GET /api/mails — list with pagination, filter by folder/account/date/read
GET /api/mails/:id — detail view
POST /api/mails/:id/action — actions (move, mark, delete)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

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
    MailSummary,
    TagResponse,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Mail

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mails", tags=["mails"])


@router.get("", response_model=list[MailSummary])
async def list_mails(
    account_id: uuid.UUID | None = Query(default=None),
    folder_id: uuid.UUID | None = Query(default=None),
    is_read: bool | None = Query(default=None),
    since: datetime | None = Query(default=None),
    before: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[MailSummary]:
    """List mails with optional filters and pagination."""
    from sqlalchemy import desc, select

    db = get_db_connection()
    async with db.session() as session:
        stmt = select(Mail).where(Mail.is_deleted.is_(False)).order_by(desc(Mail.received_at))

        if account_id is not None:
            stmt = stmt.where(Mail.account_id == account_id)
        if folder_id is not None:
            stmt = stmt.where(Mail.folder_id == folder_id)
        if is_read is not None:
            stmt = stmt.where(Mail.is_read == is_read)
        if since is not None:
            stmt = stmt.where(Mail.received_at >= since)
        if before is not None:
            stmt = stmt.where(Mail.received_at <= before)

        stmt = stmt.limit(limit).offset(offset)
        result = await session.execute(stmt)
        mails = list(result.scalars().all())

    return [MailSummary.model_validate(m) for m in mails]


@router.get("/{mail_id}", response_model=MailDetail)
async def get_mail(
    mail_id: uuid.UUID,
    account_id: uuid.UUID = Query(),
) -> MailDetail:
    """Get full mail detail by ID."""
    mail_repo = get_mail_repo()
    mail = await mail_repo.get_by_id(account_id, mail_id)
    if mail is None:
        raise HTTPException(status_code=404, detail="Mail not found")

    tag_repo = get_tag_repo()
    tags = await tag_repo.get_tags_for_mail(mail_id)

    attachment_repo = get_attachment_repo()
    attachments = await attachment_repo.get_by_mail_id(mail_id)

    from mail_verdict.core.sanitizer import sanitize_email_html

    sanitized_html = sanitize_email_html(mail.body_html) if mail.body_html else None

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
