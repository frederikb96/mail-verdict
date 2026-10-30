"""
Prompt budgets for extracted text: how much of a mail body or a document a
model reads. The numbers come from a measurement over 130 real orders: no
transactional document or ordinary mail exceeded 12,000 characters, the last
key marker (total, order or ticket number, IBAN, ticket code) of 37 of 43
transactional documents sits in their final 30% -- a head-only cut would lose
exactly that -- and terms and conditions (12,000 to 111,000 characters) hold
the operator's identity at the head and governing law and contacts at the tail
with boilerplate between.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

WHOLE_CHARS = 12_000
HEAD_CHARS = 8_000
TAIL_CHARS = 4_000
TERMS_HEAD_CHARS = 2_000
TERMS_TAIL_CHARS = 1_000
# What a document contributes to the decide call: the identifiers a mail
# carries sit at the end of a document as often as at its start.
EXCERPT_HEAD_CHARS = 2_000
EXCERPT_TAIL_CHARS = 1_000

# A cut moves up to this far to land on a line or word boundary.
_SNAP_CHARS = 200
_TERMS_RE = re.compile(
    r"AGB|Allgemeine\s+Gesch|Widerruf|Datenschutz|Terms|Nutzungsbedingungen"
    r"|Beförderungsbedingungen",
    re.IGNORECASE,
)
_TERMS_PROBE_CHARS = 600


@dataclass(frozen=True)
class Truncated:
    """`text` as the model reads it; `omitted` is the characters left out."""

    text: str
    omitted: int


def is_terms_like(filename: str, text: str) -> bool:
    """Whether a document is terms and conditions, judged by its file name or
    the start of its text."""
    return bool(_TERMS_RE.search(filename) or _TERMS_RE.search(text[:_TERMS_PROBE_CHARS]))


def _head_cut(text: str, size: int) -> int:
    """Index to cut the head at: at most `size`, pulled back to a break."""
    for sep in ("\n", " "):
        pos = text.rfind(sep, max(0, size - _SNAP_CHARS), size)
        if pos >= 0:
            return pos
    return size


def _tail_cut(text: str, size: int) -> int:
    """Index the tail starts at: at most `size` from the end, pushed to a break."""
    start = len(text) - size
    for sep in ("\n", " "):
        pos = text.find(sep, start, start + _SNAP_CHARS)
        if pos >= 0:
            return pos + 1
    return start


def truncate_text(text: str, *, terms_like: bool = False) -> Truncated:
    """
    Cut text to the prompt budget with an explicit omission marker.

    Up to `WHOLE_CHARS` is kept whole (terms-like text: up to head plus tail).
    Above, the head and the tail are kept and `[... N characters omitted ...]`
    stands between them.

    Args:
        text: The body or document text
        terms_like: Whether the text is terms and conditions (`is_terms_like`)

    Returns:
        The text as the model reads it and the number of characters left out
    """
    head, tail = (TERMS_HEAD_CHARS, TERMS_TAIL_CHARS) if terms_like else (HEAD_CHARS, TAIL_CHARS)
    return _head_and_tail(text, head, tail, limit=head + tail if terms_like else WHOLE_CHARS)


def excerpt(text: str) -> Truncated:
    """
    The short form of a document the decide call reads: whole up to head plus
    tail, else the first `EXCERPT_HEAD_CHARS` and the last `EXCERPT_TAIL_CHARS`
    with an omission marker between.
    """
    return _head_and_tail(
        text, EXCERPT_HEAD_CHARS, EXCERPT_TAIL_CHARS,
        limit=EXCERPT_HEAD_CHARS + EXCERPT_TAIL_CHARS,
    )


def _head_and_tail(text: str, head: int, tail: int, *, limit: int) -> Truncated:
    if len(text) <= limit:
        return Truncated(text, 0)
    head_end = _head_cut(text, head)
    tail_start = _tail_cut(text, tail)
    omitted = tail_start - head_end
    marker = f"[... {omitted} characters omitted ...]"
    return Truncated(f"{text[:head_end].rstrip()}\n{marker}\n{text[tail_start:].lstrip()}", omitted)
