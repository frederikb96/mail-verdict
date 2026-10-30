"""
Deterministic reading of attachments for the orders pipeline: PDF text,
barcodes with their place in the text, Tesseract for pages with no text layer,
wallet passes, calendar files, e-invoices and HTML. No model is involved, so
the same file always gives the same document.

Importing this package stays light: `extract_bytes` (PDFium, zxing-cpp, Pillow)
loads on first use, so the API process that converts mail bodies never carries
those libraries -- the pool's child process does (`pool.py`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mail_verdict.orders.extract.model import (
    CODE_MARKER_RE,
    Barcode,
    ExtractedDocument,
    Method,
)
from mail_verdict.orders.extract.truncate import Truncated, is_terms_like, truncate_text

if TYPE_CHECKING:
    from mail_verdict.orders.extract.reader import extract_bytes

__all__ = [
    "CODE_MARKER_RE", "EXTRACTOR_VERSION", "Barcode", "ExtractedDocument", "Method",
    "Truncated", "extract_bytes", "is_terms_like", "truncate_text",
]

# Stored with every extracted document. Raising it makes the next rewrite
# of an order re-read documents extracted by an older reader.
EXTRACTOR_VERSION = 1


def __getattr__(name: str) -> Any:
    if name == "extract_bytes":
        from mail_verdict.orders.extract.reader import extract_bytes

        return extract_bytes
    raise AttributeError(name)
