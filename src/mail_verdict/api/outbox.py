"""
Outbox API endpoints.

POST /api/outbox — send a message or save a draft; inserting an outbox row
  is the only way this application originates mail (there is no INSERT
  grant on messages -- see postimap/actions.py). JSON when there are no
  attachments, multipart/form-data (a "data" field holding the same JSON
  body, plus repeated "attachments" file fields) when there are.
  replaces_message_id edits or sends an existing draft in place, leaving
  no duplicate behind (requires PostIMAP >= 1.4.0).
GET /api/outbox — list outbox rows, for the outbox/status view
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import desc, select
from starlette.datastructures import UploadFile

from mail_verdict.api.schemas import (
    OutboxAttachmentSummary,
    OutboxCreateRequest,
    OutboxResponse,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Outbox, OutboxAttachment
from mail_verdict.postimap.actions import insert_outbox
from mail_verdict.postimap.contract import read_postimap_info, supports_draft_edit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/outbox", tags=["outbox"])

_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


_AttachmentTuple = tuple[str, str | None, bytes]


async def _parse_request(
    request: Request,
) -> tuple[OutboxCreateRequest, list[_AttachmentTuple]]:
    """Parse either a JSON body or a multipart form into (payload, attachments)."""
    content_type = request.headers.get("content-type", "")

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        raw_data = form.get("data")
        if not isinstance(raw_data, str):
            raise HTTPException(status_code=400, detail="Missing 'data' field in multipart body")
        payload = OutboxCreateRequest.model_validate_json(raw_data)

        attachments: list[tuple[str, str | None, bytes]] = []
        for value in form.getlist("attachments"):
            if not isinstance(value, UploadFile):
                continue
            content = await value.read()
            if len(content) > _MAX_ATTACHMENT_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Attachment '{value.filename}' exceeds the size limit",
                )
            attachments.append((value.filename or "attachment", value.content_type, content))
        return payload, attachments

    body = await request.json()
    return OutboxCreateRequest.model_validate(body), []


@router.post("", response_model=OutboxResponse, status_code=201)
async def create_outbox(request: Request) -> OutboxResponse:
    """
    Send a message or save a draft.

    A sent copy does not appear in Sent immediately -- it is transmitted
    over SMTP, appended to the account's Sent folder, and only lands in
    messages on that folder's next sync. This response is the outbox row
    to render meanwhile; its status transitions are evented over SSE as
    outbox.updated.
    """
    payload, attachments = await _parse_request(request)

    db = get_db_connection()
    async with db.session() as session:
        if payload.replaces_message_id is not None:
            info = await read_postimap_info(session)
            if info is None or not supports_draft_edit(info):
                raise HTTPException(
                    status_code=501,
                    detail=(
                        "Editing or sending a draft in place requires PostIMAP "
                        f"service_version >= 1.4.0; the running instance reports "
                        f"{info.service_version if info else 'unknown'}."
                    ),
                )

        outbox = await insert_outbox(
            session,
            account_id=payload.account_id,
            kind=payload.kind,
            to_addrs=payload.to or None,
            cc_addrs=payload.cc,
            bcc_addrs=payload.bcc,
            subject=payload.subject,
            body_text=payload.body_text,
            body_html=payload.body_html,
            in_reply_to=payload.in_reply_to,
            references=payload.references,
            replaces_message_id=payload.replaces_message_id,
            attachments=attachments,
        )
        att_result = await session.execute(
            select(OutboxAttachment).where(OutboxAttachment.outbox_id == outbox.id)
        )
        return _to_response(outbox, list(att_result.scalars().all()))


@router.get("", response_model=list[OutboxResponse])
async def list_outbox(
    account_id: uuid.UUID | None = None,
    status: str | None = None,
) -> list[OutboxResponse]:
    """List outbox rows, newest first, optionally scoped to an account/status."""
    db = get_db_connection()
    async with db.session() as session:
        stmt = select(Outbox).order_by(desc(Outbox.created_at))
        if account_id is not None:
            stmt = stmt.where(Outbox.account_id == account_id)
        if status is not None:
            stmt = stmt.where(Outbox.status == status)
        result = await session.execute(stmt)
        rows = list(result.scalars().all())

        if not rows:
            return []

        att_result = await session.execute(
            select(OutboxAttachment).where(
                OutboxAttachment.outbox_id.in_([o.id for o in rows])
            )
        )
        attachments_by_outbox: dict[uuid.UUID, list[OutboxAttachment]] = {}
        for att in att_result.scalars().all():
            attachments_by_outbox.setdefault(att.outbox_id, []).append(att)

    return [_to_response(o, attachments_by_outbox.get(o.id, [])) for o in rows]


def _to_response(outbox: Outbox, attachments: list[OutboxAttachment]) -> OutboxResponse:
    """Build an OutboxResponse, mapping DB column names to the UI's wire names."""
    return OutboxResponse(
        id=outbox.id,
        account_id=outbox.account_id,
        kind=outbox.kind,
        status=outbox.status,
        to=list(outbox.to_addrs) if outbox.to_addrs else [],
        cc=list(outbox.cc_addrs) if outbox.cc_addrs else None,
        bcc=list(outbox.bcc_addrs) if outbox.bcc_addrs else None,
        subject=outbox.subject,
        error=outbox.error,
        attachments=[
            OutboxAttachmentSummary(
                id=a.id, filename=a.filename, content_type=a.content_type,
                size_bytes=len(a.data) if a.data else None,
            )
            for a in attachments
        ],
        created_at=outbox.created_at,
        updated_at=outbox.updated_at,
    )
