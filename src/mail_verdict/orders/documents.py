"""
The documents of an order: attachments of its mails and files added by hand,
read once by the extraction pool and stored with the text and barcodes the
prompts and the wallet need.

A mail's attachments are read in memory when the decide call runs (the
result is stored only if the mail joins an order) and again by `heal_*` when
a write finds a document missing or read by an older extractor -- which is
what lets a rewrite repair an order written before documents existed.
Stored text is already cut to the prompt budget, so storage is bounded per
row. Bytes are never stored here: a mail attachment's stay in PostIMAP, a
file's in order_files.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.config import get_config
from mail_verdict.database.models import OrderDocument, OrderFile
from mail_verdict.orders.content import AttachmentRef
from mail_verdict.orders.extract import EXTRACTOR_VERSION
from mail_verdict.orders.extract.model import ExtractedDocument
from mail_verdict.orders.extract.pool import ExtractionPool
from mail_verdict.orders.extract.source import read_mail_attachment, read_order_file
from mail_verdict.orders.extract.truncate import excerpt, is_terms_like, truncate_text
from mail_verdict.orders.prompts import PromptDocument

logger = logging.getLogger(__name__)

# Characters of one document kept in order_documents.text.
_STORED_TEXT_CHARS = 20_000
# Attachments of one mail that are read at all; the rest only list on the mail.
_MAX_ATTACHMENTS_READ = 12
# The decide call shows this many attachments of the new mail.
DECIDE_ATTACHMENTS = 6
# The write prompt's total size. Over it, the middle mails go first.
WRITE_PROMPT_CEILING_CHARS = 120_000

_pool: ExtractionPool | None = None


def extraction_pool() -> ExtractionPool:
    """The process-wide extraction pool, created on first use."""
    global _pool
    if _pool is None:
        _pool = ExtractionPool(get_config().extraction)
    return _pool


def close_extraction_pool() -> None:
    """Stop the pool's child process; the next use starts a new one."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@dataclass(frozen=True)
class ReadDocument:
    """One attachment or file as the pool read it."""

    attachment_id: uuid.UUID | None
    file_id: uuid.UUID | None
    document: ExtractedDocument


def _unread(ref: AttachmentRef, error: str) -> ExtractedDocument:
    return ExtractedDocument(
        filename=ref.filename, content_type=ref.content_type, size_bytes=ref.size_bytes or 0,
        sha256="", method="failed", error=error,
    )


async def read_mail_documents(
    session: AsyncSession, attachments: Sequence[AttachmentRef],
) -> list[ReadDocument]:
    """Read a mail's attachments one after the other, in the pool's child."""
    limit = get_config().extraction.max_document_bytes
    reads: list[ReadDocument] = []
    for ref in attachments[:_MAX_ATTACHMENTS_READ]:
        if ref.size_bytes is not None and ref.size_bytes > limit:
            reads.append(ReadDocument(ref.id, None, _unread(ref, f"larger than {limit} bytes")))
            continue
        data = await read_mail_attachment(session, ref.id)
        if data is None:
            continue
        document = await extraction_pool().extract(data, ref.content_type, ref.filename)
        reads.append(ReadDocument(ref.id, None, document))
    if len(attachments) > _MAX_ATTACHMENTS_READ:
        logger.warning("A mail has %d attachments; reading the first %d", len(attachments),
                       _MAX_ATTACHMENTS_READ)
    return reads


async def read_file_document(
    session: AsyncSession, *, file_id: uuid.UUID, filename: str, content_type: str,
) -> ReadDocument | None:
    """Read an uploaded file; None when its row is gone."""
    data = await read_order_file(session, file_id)
    if data is None:
        return None
    document = await extraction_pool().extract(data, content_type, filename)
    return ReadDocument(None, file_id, document)


def _clean(value: str) -> str:
    # PostgreSQL text and jsonb hold no NUL.
    return value.replace("\x00", "")


def _row_values(read: ReadDocument) -> dict[str, Any]:
    doc = read.document
    stored = truncate_text(doc.text, terms_like=is_terms_like(doc.filename, doc.text)).text
    return {
        "filename": _clean(doc.filename), "content_type": _clean(doc.content_type),
        "size_bytes": doc.size_bytes, "sha256": doc.sha256 or None,
        "page_count": doc.page_count, "method": doc.method,
        "text": _clean(stored[:_STORED_TEXT_CHARS]), "text_chars_total": doc.text_chars_total,
        "barcodes": [_barcode_json(b.to_json()) for b in doc.barcodes],
        "extractor_version": EXTRACTOR_VERSION,
    }


def _barcode_json(entry: dict[str, object]) -> dict[str, object]:
    value = entry.get("text")
    return {**entry, "text": _clean(value) if isinstance(value, str) else None}


async def store_mail_documents(
    session: AsyncSession, *, order_id: uuid.UUID, account_id: uuid.UUID, msg_key: str,
    reads: Sequence[ReadDocument],
) -> None:
    """Store (or refresh) the documents of one mail of an order."""
    for read in reads:
        values = _row_values(read)
        stmt = pg_insert(OrderDocument).values(
            order_id=order_id, source="mail", account_id=account_id, msg_key=msg_key,
            attachment_id=read.attachment_id, **values,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["account_id", "msg_key", "attachment_id"],
            index_where=text("source = 'mail'"),
            set_={"order_id": order_id, "extracted_at": text("now()"), **values},
        )
        await session.execute(stmt)


async def store_file_document(
    session: AsyncSession, *, order_id: uuid.UUID, read: ReadDocument,
) -> uuid.UUID:
    """Store (or refresh) the document of an uploaded file; returns its id."""
    values = _row_values(read)
    insert_stmt = pg_insert(OrderDocument).values(
        order_id=order_id, source="file", file_id=read.file_id, **values,
    )
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=["file_id"], index_where=text("source = 'file'"),
        set_={"order_id": order_id, "extracted_at": text("now()"), **values},
    ).returning(OrderDocument.id)
    return uuid.UUID(str((await session.execute(stmt)).scalar_one()))


async def heal_mail_documents(
    session: AsyncSession, *, order_id: uuid.UUID, account_id: uuid.UUID, msg_key: str,
    attachments: Sequence[AttachmentRef],
) -> int:
    """
    Make sure every live attachment of a mail of the order has a document
    read by the current extractor. A document whose attachment id no longer
    exists (a resync gives a mail's attachments new ids) is matched by name,
    type and size and re-pointed rather than read again.

    Returns:
        How many attachments were read
    """
    result = await session.execute(
        select(
            OrderDocument.id, OrderDocument.attachment_id, OrderDocument.filename,
            OrderDocument.content_type, OrderDocument.size_bytes,
            OrderDocument.extractor_version,
        ).where(
            OrderDocument.source == "mail", OrderDocument.account_id == account_id,
            OrderDocument.msg_key == msg_key,
        )
    )
    existing = list(result.all())
    live_ids = {ref.id for ref in attachments}
    by_attachment = {row.attachment_id: row for row in existing}
    orphans = [row for row in existing if row.attachment_id not in live_ids]

    to_read: list[AttachmentRef] = []
    for ref in attachments[:_MAX_ATTACHMENTS_READ]:
        row = by_attachment.get(ref.id)
        if row is None:
            row = next(
                (
                    o for o in orphans
                    if (o.filename, o.content_type, o.size_bytes)
                    == (ref.filename, ref.content_type, ref.size_bytes)
                ),
                None,
            )
            if row is not None:
                orphans.remove(row)
                await session.execute(
                    update(OrderDocument).where(OrderDocument.id == row.id)
                    .values(attachment_id=ref.id)
                )
        if row is None or row.extractor_version < EXTRACTOR_VERSION:
            to_read.append(ref)
    if not to_read:
        return 0
    reads = await read_mail_documents(session, to_read)
    await store_mail_documents(
        session, order_id=order_id, account_id=account_id, msg_key=msg_key, reads=reads,
    )
    return len(reads)


async def heal_file_documents(session: AsyncSession, order_id: uuid.UUID) -> int:
    """Read the attached files of the order that have no current document."""
    result = await session.execute(
        select(OrderFile.id, OrderFile.filename, OrderFile.content_type)
        .outerjoin(
            OrderDocument,
            (OrderDocument.file_id == OrderFile.id) & (OrderDocument.source == "file"),
        )
        .where(
            OrderFile.order_id == order_id, OrderFile.status == "attached",
            (OrderDocument.id.is_(None)) | (OrderDocument.extractor_version < EXTRACTOR_VERSION),
        )
    )
    healed = 0
    for file_id, filename, content_type in result.all():
        read = await read_file_document(
            session, file_id=file_id, filename=filename, content_type=content_type,
        )
        if read is not None:
            await store_file_document(session, order_id=order_id, read=read)
            healed += 1
    return healed


# --- Prompt assembly -------------------------------------------------------

_MARKER_START_RE = re.compile(r"\[code b(\d+) ")


def _number_markers(document_text: str, index: int) -> str:
    """`[code b1 ...]` as the prompt names it: `[code d<index>:b1 ...]`."""
    return _MARKER_START_RE.sub(lambda m: f"[code d{index}:b{m.group(1)} ", document_text)


def decide_documents(reads: Sequence[ReadDocument]) -> list[PromptDocument]:
    """The new mail's (or file's) attachments as the decide call reads them:
    an excerpt each, numbered from 1."""
    shown = [r for r in reads if r.document.text or r.document.barcodes][:DECIDE_ATTACHMENTS]
    return [
        PromptDocument(
            index=i, name=r.document.filename, content_type=r.document.content_type,
            page_count=r.document.page_count, method=r.document.method,
            text=_number_markers(excerpt(r.document.text).text, i),
        )
        for i, r in enumerate(shown, start=1)
    ]


def attachment_haystack(reads: Sequence[ReadDocument]) -> str:
    """The attachments' whole text, for the number search."""
    return "\n".join(r.document.text for r in reads if r.document.text)


@dataclass(frozen=True)
class StoredDocument:
    """An order document as a write prompt needs it (no barcodes, no bytes)."""

    id: uuid.UUID
    source: str
    account_id: uuid.UUID | None
    msg_key: str | None
    file_id: uuid.UUID | None
    filename: str
    content_type: str
    page_count: int | None
    method: str
    text: str
    added_at: datetime | None


async def load_stored_documents(session: AsyncSession, order_id: uuid.UUID) -> list[StoredDocument]:
    result = await session.execute(
        select(
            OrderDocument.id, OrderDocument.source, OrderDocument.account_id,
            OrderDocument.msg_key, OrderDocument.file_id, OrderDocument.filename,
            OrderDocument.content_type, OrderDocument.page_count, OrderDocument.method,
            OrderDocument.text, OrderFile.created_at,
        )
        .outerjoin(OrderFile, OrderFile.id == OrderDocument.file_id)
        .where(OrderDocument.order_id == order_id)
        .order_by(OrderDocument.filename, OrderDocument.id)
    )
    return [StoredDocument(*row) for row in result.all()]


@dataclass(frozen=True)
class WriteInput:
    """What the write prompt shows: mails with their numbered documents,
    files, and which `d<k>:b<n>` marker names which stored barcode."""

    mails: list[dict[str, Any]]
    files: list[PromptDocument]
    code_map: dict[str, tuple[uuid.UUID, int]]


def _chars(mail: dict[str, Any]) -> int:
    return len(mail["body"]) + sum(len(d.text) for d in mail["stored_documents"])


def assemble_write_input(
    mails: list[dict[str, Any]], documents: Sequence[StoredDocument],
    *, ceiling: int = WRITE_PROMPT_CEILING_CHARS,
) -> WriteInput:
    """
    Attach each mail's stored documents, drop middle mails until the prompt
    fits `ceiling`, then number every document that remains (mails in order,
    then files) and rewrite its code markers to that number.

    Args:
        mails: Oldest first; each has `account_id` and `msg_key` besides the
            fields prompts.build_write_user_prompt reads
        documents: The order's stored documents
        ceiling: Largest total of body and document characters
    """
    by_mail: dict[tuple[uuid.UUID | None, str | None], list[StoredDocument]] = {}
    file_documents: list[StoredDocument] = []
    for doc in documents:
        if doc.source == "file":
            file_documents.append(doc)
        elif doc.text.strip():
            # A mail's logos and spacer images read as empty text: listing
            # them only numbers blank blocks. A file added by hand stays, so
            # the model knows it exists.
            by_mail.setdefault((doc.account_id, doc.msg_key), []).append(doc)
    file_documents.sort(key=lambda d: (d.added_at or datetime.min, d.filename))

    kept = [
        {**mail, "stored_documents": by_mail.get((mail["account_id"], mail["msg_key"]), [])}
        for mail in mails
    ]
    files_chars = sum(len(d.text) for d in file_documents)
    while len(kept) > 2 and sum(_chars(m) for m in kept) + files_chars > ceiling:
        del kept[len(kept) // 2]

    code_map: dict[str, tuple[uuid.UUID, int]] = {}
    counter = 0

    def number(doc: StoredDocument) -> PromptDocument:
        nonlocal counter
        counter += 1
        for match in _MARKER_START_RE.finditer(doc.text):
            code_map[f"d{counter}:b{match.group(1)}"] = (doc.id, int(match.group(1)))
        return PromptDocument(
            index=counter, name=doc.filename, content_type=doc.content_type,
            page_count=doc.page_count, method=doc.method,
            text=_number_markers(doc.text, counter),
            added=doc.added_at.date().isoformat() if doc.added_at else "",
        )

    prompt_mails = []
    for mail in kept:
        stored = mail.pop("stored_documents")
        prompt_mails.append({**mail, "documents": [number(d) for d in stored]})
    files = [number(d) for d in file_documents]
    return WriteInput(mails=prompt_mails, files=files, code_map=code_map)
