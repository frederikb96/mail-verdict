"""
Outbox API endpoints.

POST /api/outbox — send a message or save a draft; inserting an outbox row
  is the only way this application originates mail (there is no INSERT
  grant on messages -- see postimap/actions.py). JSON when there are no
  attachments, multipart/form-data (a "data" field holding the same JSON
  body, plus repeated "attachments" file fields) when there are.
  replaces_message_id edits or sends an existing draft in place, leaving
  no duplicate behind (requires PostIMAP >= 1.4.0). identity_id resolves
  through api/identities.py to the from_addr the row is actually written
  with, falling back to the account's default identity and then to
  accounts.imap_user (PostIMAP's own fallback) if it has none. body_html
  has any quoted image's display-only placeholder restored to a real URL
  (see core/image_sanitizer.py), then is passed through
  core/outbound_sanitizer.py before it reaches insert_outbox() -- the
  boundary that makes composed, pasted and quoted content safe to send --
  and requires body_text alongside it, since nothing derives a text/plain
  alternative from HTML on this producer's behalf. The MCP
  send_mail/draft_mail tools only ever accept body_text, so neither sees
  this path.
GET /api/outbox — list outbox rows, for the outbox/status view
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import desc, select
from starlette.datastructures import UploadFile

from mail_verdict.api.identities import resolve_send_from_addr
from mail_verdict.api.schemas import (
    OutboxAttachmentSummary,
    OutboxCreateRequest,
    OutboxResponse,
)
from mail_verdict.config import get_config
from mail_verdict.core.image_sanitizer import restore_remote_images
from mail_verdict.core.outbound_sanitizer import sanitize_outbound_html
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Message, Outbox, OutboxAttachment
from mail_verdict.postimap.actions import insert_outbox
from mail_verdict.postimap.contract import read_postimap_info, supports_draft_edit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/outbox", tags=["outbox"])

_AttachmentTuple = tuple[str, str | None, bytes]

# Read size for the chunked cap in _read_capped_attachment -- arbitrary,
# just small enough that an oversized upload is caught within a few reads
# rather than after the whole thing is buffered.
_ATTACHMENT_READ_CHUNK_BYTES = 1024 * 1024


async def _read_capped_attachment(value: UploadFile, max_bytes: int) -> bytes:
    """
    Read one upload, aborting as soon as it exceeds max_bytes.

    config/config.yaml documents this limit as enforced while the upload
    is being read, because the cost being bounded is memory -- a plain
    `await value.read()` followed by a length check buffers the entire
    file first, which bounds nothing for an attachment larger than the
    limit. Reading in chunks and stopping the moment the running total
    crosses max_bytes keeps that promise.

    Args:
        value: The uploaded file to read
        max_bytes: The per-attachment size limit (outbox.max_attachment_bytes)

    Returns:
        The attachment's bytes

    Raises:
        HTTPException: 413 if the upload exceeds max_bytes
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await value.read(_ATTACHMENT_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Attachment '{value.filename}' exceeds the size limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _parse_request(
    request: Request,
) -> tuple[OutboxCreateRequest, list[_AttachmentTuple]]:
    """Parse either a JSON body or a multipart form into (payload, attachments).

    The body is validated here rather than through a declared parameter,
    because the same endpoint accepts JSON and multipart. FastAPI only turns a
    ValidationError into a 422 for bodies it binds itself, so validating by
    hand means catching it by hand -- otherwise a malformed field reaches the
    client as a 500, telling it the server is broken when its request was.
    """
    content_type = request.headers.get("content-type", "")

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        raw_data = form.get("data")
        if not isinstance(raw_data, str):
            raise HTTPException(status_code=400, detail="Missing 'data' field in multipart body")
        try:
            payload = OutboxCreateRequest.model_validate_json(raw_data)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc

        limits = get_config().outbox
        uploads = [v for v in form.getlist("attachments") if isinstance(v, UploadFile)]
        if len(uploads) > limits.max_attachments:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"{len(uploads)} attachments exceeds the limit of "
                    f"{limits.max_attachments}"
                ),
            )

        attachments: list[tuple[str, str | None, bytes]] = []
        total = 0
        for value in uploads:
            content = await _read_capped_attachment(value, limits.max_attachment_bytes)
            total += len(content)
            # Checked as the total grows rather than at the end: the point is
            # to stop reading, not to discover afterwards how much was read.
            if total > limits.max_attachments_total_bytes:
                raise HTTPException(
                    status_code=413,
                    detail="Attachments exceed the total size limit for one message",
                )
            attachments.append((value.filename or "attachment", value.content_type, content))
        return payload, attachments

    try:
        body = await request.json()
    except ValueError as exc:
        # A body that is not JSON at all is still the client's mistake, and
        # json() raises before any field is looked at -- so it needs its own
        # answer rather than falling through to the generic handler.
        raise HTTPException(status_code=400, detail=f"Body is not valid JSON: {exc}") from exc

    try:
        return OutboxCreateRequest.model_validate(body), []
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


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

    if payload.body_html and not payload.body_text:
        # Nodemailer does not derive a text/plain alternative from HTML on
        # its own, so a producer sending body_html with no body_text would
        # ship a message with no text alternative at all -- a bad citizen
        # for any client or filter that only reads the plain part. Every
        # producer is required to send both rather than this endpoint
        # deriving one on their behalf, which would be a second place
        # computing the same content.
        raise HTTPException(
            status_code=400, detail="body_text is required whenever body_html is set",
        )
    # A quoted reply or forward may still carry the display-only
    # data-x-src/data-x-style placeholder the quote endpoint (api/mails.py)
    # rewrites remote images to, unrestored if the quoted sender was never
    # allowlisted -- restoring it here, on every outbox row regardless of
    # whether it is sent immediately or only saved as a draft, is what
    # keeps a quoted image reaching whoever this goes to, independent of
    # this account's own allowlist. sanitize_outbound_html has no
    # allowlist entry for either placeholder and would drop the image
    # outright rather than pass it through unrecognised.
    body_html = (
        sanitize_outbound_html(restore_remote_images(payload.body_html))
        if payload.body_html
        else None
    )

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

            # The column carries a real foreign key onto messages, so an id
            # that resolves to nothing is a constraint violation at insert
            # time rather than a value PostIMAP ignores. Answer it as the
            # client error it is.
            superseded = await session.scalar(
                select(Message.account_id).where(
                    Message.id == payload.replaces_message_id,
                    Message.expunged_at.is_(None),
                )
            )
            if superseded is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"No message {payload.replaces_message_id} to supersede; "
                        "it does not exist or has already been removed."
                    ),
                )
            if superseded != payload.account_id:
                raise HTTPException(
                    status_code=400,
                    detail="A draft can only be superseded within its own account.",
                )

        from_addr = await resolve_send_from_addr(
            session, payload.account_id, payload.identity_id,
        )
        outbox = await insert_outbox(
            session,
            account_id=payload.account_id,
            kind=payload.kind,
            from_addr=from_addr,
            to_addrs=payload.to or None,
            cc_addrs=payload.cc,
            bcc_addrs=payload.bcc,
            subject=payload.subject,
            body_text=payload.body_text,
            body_html=body_html,
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
        from_addr=outbox.from_addr,
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
