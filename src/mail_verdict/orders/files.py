"""
Files added to the orders register by hand: what is accepted, and the rows
and jobs an upload creates. The bytes live in `order_files` (owned, never
selected in bulk); the file job in orders/worker.py reads them, places the
file in an entry and stores its document.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import Order, OrderFile, OrderJob
from mail_verdict.orders import repository
from mail_verdict.queue.notify import WorkQueueNotifier

# Type name -> the content type stored. Every upload gets one: the types
# below are the ones read for text and codes (or, `heic`, kept as the photo it
# is), and `other` is any other file, stored and downloaded as it came.
FILE_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "tiff": "image/tiff",
    "bmp": "image/bmp",
    "heic": "image/heic",
    "pkpass": "application/vnd.apple.pkpass",
    "ics": "text/calendar",
    "txt": "text/plain",
    "html": "text/html",
    "xml": "application/xml",
    "other": "application/octet-stream",
}

_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1")

# Control bytes that never occur in text: everything below space except
# tab, line feed, form feed and carriage return.
_BINARY_BYTES = bytes([*range(0, 9), 11, *range(14, 32)])

_MAX_FILENAME_CHARS = 200
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_UPLOAD_PRIORITY = 5


def detect_file_type(filename: str, head: bytes) -> str:
    """
    The type of an upload (a key of FILE_TYPES), judged by its first bytes
    and its name -- the declared content type is not trusted. A file that
    matches nothing is `other`: it is stored and listed, not read.
    """
    name = filename.lower()
    start = head.lstrip()
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if head.startswith(b"BM") and name.endswith(".bmp"):
        return "bmp"
    if head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        return "heic"
    if name.endswith(".pkpass") and head.startswith(b"PK"):
        return "pkpass"
    if name.endswith(".ics") or start.upper().startswith(b"BEGIN:VCALENDAR"):
        return "ics"
    if name.endswith(".xml") or start.startswith(b"<?xml"):
        return "xml"
    if name.endswith((".html", ".htm")) or start[:15].lower().startswith(
        (b"<!doctype html", b"<html"),
    ):
        return "html"
    if name.endswith(".txt") or _looks_like_text(head):
        return "txt"
    return "other"


def _looks_like_text(head: bytes) -> bool:
    """Whether the first bytes are text: something there, and none of the
    control bytes a binary format carries within a few kilobytes."""
    return bool(head) and len(head.translate(None, _BINARY_BYTES)) == len(head)


def clean_filename(name: str | None) -> str:
    """A file name safe to store and to put in a header: no directories, no
    control characters, bounded."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _CONTROL_RE.sub("", base).strip()
    return base[:_MAX_FILENAME_CHARS] or "file"


@dataclass(frozen=True)
class FileState:
    file_id: uuid.UUID
    order_id: uuid.UUID | None
    status: str
    last_error: str | None


async def entry_usage(session: AsyncSession, order_id: uuid.UUID) -> tuple[int, int]:
    """(files, total bytes) the entry already holds."""
    result = await session.execute(
        select(func.count(OrderFile.id), func.coalesce(func.sum(OrderFile.size_bytes), 0))
        .where(OrderFile.order_id == order_id)
    )
    count, total = result.one()
    return int(count), int(total)


async def create_file(
    session: AsyncSession, *, filename: str, content_type: str, data: bytes,
    order_id: uuid.UUID | None,
) -> FileState:
    """
    Store an upload and queue its file job. The same bytes uploaded again
    while the first copy is waiting or placed answer with that copy (a share
    sheet retrying a flaky upload must not add a second document).
    """
    digest = hashlib.sha256(data).hexdigest()
    same = [OrderFile.sha256 == digest, OrderFile.status.in_(("pending", "attached"))]
    if order_id is not None:
        same.append(OrderFile.order_id == order_id)
    duplicate = await session.execute(
        select(OrderFile.id, OrderFile.order_id, OrderFile.status, OrderFile.last_error)
        .where(*same).limit(1)
    )
    existing = duplicate.first()
    if existing is not None:
        return FileState(existing.id, existing.order_id, existing.status, existing.last_error)

    result = await session.execute(
        pg_insert(OrderFile).values(
            order_id=order_id, filename=filename, content_type=content_type,
            size_bytes=len(data), sha256=digest, data=data, status="pending",
        ).returning(OrderFile.id)
    )
    file_id = uuid.UUID(str(result.scalar_one()))
    await session.execute(
        pg_insert(OrderJob)
        .values(kind="file", file_id=file_id, origin="upload", priority=_UPLOAD_PRIORITY)
        .on_conflict_do_nothing(index_elements=["file_id"], index_where=text("kind = 'file'"))
    )
    await WorkQueueNotifier.notify(session, "orders")
    return FileState(file_id, order_id, "pending", None)


async def file_state(session: AsyncSession, file_id: uuid.UUID) -> FileState | None:
    result = await session.execute(
        select(OrderFile.id, OrderFile.order_id, OrderFile.status, OrderFile.last_error)
        .where(OrderFile.id == file_id)
    )
    row = result.first()
    return FileState(row.id, row.order_id, row.status, row.last_error) if row else None


async def delete_file(
    session: AsyncSession, order_id: uuid.UUID, file_id: uuid.UUID,
) -> bool | None:
    """
    Delete one file of an entry: its document and code facts go with it, the
    entry's aggregates are recomputed, and the entry is deleted when nothing
    is left of it, else rewritten.

    Returns:
        None when the entry has no such file; whether the entry was deleted
    """
    deleted = await session.execute(
        delete(OrderFile).where(OrderFile.id == file_id, OrderFile.order_id == order_id)
        .returning(OrderFile.id)
    )
    if deleted.first() is None:
        return None
    if await repository.recompute_aggregates(session, order_id) == 0:
        return await repository.delete_order_if_empty(session, order_id)
    await repository.mark_text_stale(session, order_id)
    await repository.enqueue_write_job(session, order_id, priority=10, origin="manual")
    return False


async def prune_failed_files(session: AsyncSession, *, days: int) -> int:
    """Delete files the register failed to place, `days` after they were added."""
    result = await session.execute(
        text(
            "DELETE FROM order_files WHERE status = 'failed' "
            "AND created_at < now() - make_interval(days => :days) RETURNING id"
        ),
        {"days": days},
    )
    return len(result.all())


async def order_exists(session: AsyncSession, order_id: uuid.UUID) -> bool:
    result = await session.execute(select(Order.id).where(Order.id == order_id))
    return result.scalar_one_or_none() is not None
