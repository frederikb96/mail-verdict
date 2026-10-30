"""
`extract_bytes`: one attachment in, one `ExtractedDocument` out, dispatching on
what the file is. Synchronous and CPU-heavy -- callers go through `pool.py`,
which runs it in a child process.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from typing import Any

from mail_verdict.config.loader import ExtractionConfig
from mail_verdict.orders.extract.html_text import html_to_text
from mail_verdict.orders.extract.ics_text import read_ics
from mail_verdict.orders.extract.images import read_image
from mail_verdict.orders.extract.model import Barcode, ExtractedDocument, Read
from mail_verdict.orders.extract.pdf import read_pdf
from mail_verdict.orders.extract.pkpass import read_pkpass
from mail_verdict.orders.extract.xml_invoice import read_invoice_xml

logger = logging.getLogger(__name__)

_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*")
# Image types Pillow reads. Any other image/* label (HEIC, SVG, ...) is stored
# as a document without being read, rather than failing on every attempt.
_IMAGE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/pjpeg", "image/jpg", "image/gif", "image/webp",
    "image/tiff", "image/bmp", "image/x-ms-bmp",
})


def _is_image(data: bytes, ctype: str) -> bool:
    return (
        data.startswith(_IMAGE_MAGIC)
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        or ctype in _IMAGE_TYPES
    )


def _kind(data: bytes, content_type: str, filename: str) -> str | None:
    """The reader a file needs, by its content first and its labels second."""
    ctype = content_type.split(";")[0].strip().lower()
    name = filename.lower()
    head = data[:4096].lstrip()
    if data.startswith(b"%PDF-") or ctype == "application/pdf":
        return "pdf"
    if name.endswith(".pkpass") or ctype == "application/vnd.apple.pkpass":
        return "pkpass"
    if head.upper().startswith(b"BEGIN:VCALENDAR") or name.endswith(".ics") or "calendar" in ctype:
        return "ics"
    if _is_image(data, ctype):
        return "image"
    if head.startswith(b"<?xml") or name.endswith(".xml") or ctype.endswith("xml"):
        return "xml"
    if name.endswith((".html", ".htm")) or ctype == "text/html" or head[:15].lower().startswith(
        (b"<!doctype html", b"<html"),
    ):
        return "html"
    if ctype == "text/plain" or name.endswith(".txt"):
        return "text"
    return None


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _read(kind: str | None, data: bytes, limits: ExtractionConfig) -> Read:
    if kind == "pdf":
        return read_pdf(data, limits)
    if kind == "pkpass":
        return read_pkpass(data)
    if kind == "ics":
        return read_ics(data)
    if kind == "image":
        return read_image(data, limits)
    if kind == "xml":
        return read_invoice_xml(data)
    if kind == "html":
        return Read(method="html", text=html_to_text(_decode(data)))
    if kind == "text":
        return Read(method="text_layer", text=_decode(data).strip())
    return Read(method="none", text="")


def extract_bytes(
    data: bytes, content_type: str, filename: str, limits: ExtractionConfig,
) -> ExtractedDocument:
    """
    Read one attachment.

    A file that cannot be read -- too large, corrupt, an OCR failure -- comes
    back as method `failed` with the reason in `error`, never as an exception,
    so one bad attachment cannot stop the others.

    Args:
        data: The file's bytes
        content_type: Its declared MIME type (content wins over a wrong label)
        filename: Its file name
        limits: The `extraction` section of config.yaml

    Returns:
        The document with its text, barcodes and method
    """
    def build(**fields: Any) -> ExtractedDocument:
        return ExtractedDocument(
            filename=filename, content_type=content_type, size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(), **fields,
        )

    if len(data) > limits.max_document_bytes:
        return build(method="failed", error=f"larger than {limits.max_document_bytes} bytes")
    try:
        read = _read(_kind(data, content_type, filename), data, limits)
    except Exception as exc:
        logger.warning("Extraction of %r failed: %s: %s", filename, type(exc).__name__, exc)
        return build(method="failed", error=f"{type(exc).__name__}: {exc}")

    text = read.text.strip()
    kept = text[: limits.max_text_chars]
    barcodes = tuple(
        b if b.text_offset is None or b.text_offset < len(kept) else _unplaced(b)
        for b in read.barcodes
    )
    return build(
        method=read.method, page_count=read.page_count, text=kept,
        text_chars_total=len(text), barcodes=barcodes,
    )


def _unplaced(code: Barcode) -> Barcode:
    return replace(code, text_offset=None)
