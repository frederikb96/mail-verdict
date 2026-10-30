"""
The result types of document extraction: what one attachment yielded, as
plain data the child process can pickle back and the repository can store.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Literal

Method = Literal[
    "text_layer", "ocr", "pkpass", "ics", "xml", "html", "image", "none", "failed",
]

# A barcode placeholder inside extracted text: `[code b2 QR "5471205382"]` or
# `[code b1 Aztec 290 bytes]`. `b<N>` is the barcode's `index` in its document.
CODE_MARKER_RE = re.compile(r"\[code b(\d+) [^\]\n]*\]")


@dataclass(frozen=True)
class Barcode:
    """One decoded code. `payload` is the exact bytes (a DB ticket's Aztec is
    binary and its text rendering is lossy); `text` is set only when the payload
    is printable. `page`, `bbox` (left, top, right, bottom, in page points) and
    `text_offset` (where the marker sits in the document text) are None for a
    code that has no place on a page, such as a pass's."""

    index: int
    format: str
    payload: bytes
    text: str | None
    page: int | None
    bbox: tuple[float, float, float, float] | None
    text_offset: int | None
    crop_png: bytes | None = None

    def to_json(self) -> dict[str, object]:
        """The JSON shape stored in `order_documents.barcodes`."""
        return {
            "index": self.index,
            "format": self.format,
            "text": self.text,
            "bytes_b64": base64.b64encode(self.payload).decode("ascii"),
            "page": self.page,
            "bbox": list(self.bbox) if self.bbox else None,
            "text_offset": self.text_offset,
            "crop_b64": base64.b64encode(self.crop_png).decode("ascii")
            if self.crop_png else None,
        }


@dataclass(frozen=True)
class ExtractedDocument:
    """What one attachment gave. `text` holds the whole readable text up to the
    configured hard ceiling, with a code marker at each barcode's reading-order
    position; `text_chars_total` is its length before that ceiling. Prompt
    budgets are applied separately (`truncate.py`)."""

    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    method: Method
    page_count: int | None = None
    text: str = ""
    text_chars_total: int = 0
    barcodes: tuple[Barcode, ...] = field(default_factory=tuple)
    error: str | None = None


@dataclass(frozen=True)
class Read:
    """What one type-specific reader returns; `extract_bytes` wraps it with
    the file's identity into an `ExtractedDocument`."""

    method: Method
    text: str
    page_count: int | None = None
    barcodes: tuple[Barcode, ...] = field(default_factory=tuple)
