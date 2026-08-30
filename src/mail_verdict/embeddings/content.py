"""
Building the text a message is embedded from.

Subject and sender carry the most discriminating signal in a short vector,
so they are always first; the body is truncated because most of a message's
length is quoted reply chains, HTML boilerplate and signatures that dilute
a single 1536-dimension vector rather than sharpening it.

A message with no usable body -- truncated by PostIMAP's own size cap, or
genuinely empty -- still gets a vector from subject and sender alone rather
than being skipped. A message with no vector at all is invisible to
semantic search, which is a worse failure than a weak one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import nh3

CONTENT_LEVEL_FULL = "full"
CONTENT_LEVEL_ENVELOPE = "envelope"


@dataclass(frozen=True)
class EmbeddingInput:
    """The exact text to embed, and what it says about how it was built."""

    text: str
    content_level: str
    source_hash: str


def _strip_html(html: str) -> str:
    """Reduce HTML to its text content.

    nh3 with an empty allowed-tag set removes every tag's markup while
    keeping the text between them, which is what an embedding wants -- the
    sanitizer used for rendering (core/sanitizer.py) keeps a much larger
    whitelist because it produces HTML for display, not plain text.
    """
    return nh3.clean(html, tags=set(), attributes={})


def build_embedding_input(
    *,
    subject: str | None,
    from_addr: str | None,
    body_text: str | None,
    body_html: str | None,
    is_truncated: bool,
    content_chars: int,
) -> EmbeddingInput:
    """
    Build the text to embed for one message.

    Args:
        subject: Message subject
        from_addr: Envelope sender
        body_text: Plain-text body, or None if absent or never fetched
        body_html: HTML body, used only when body_text is absent
        is_truncated: True when PostIMAP never fetched the body at all --
            body_text/body_html are meaningless in that case regardless of
            their actual value
        content_chars: Maximum characters of body content to include

    Returns:
        The text to embed, which content level it reflects, and a hash of
        that exact text for skip-if-unchanged re-encoding
    """
    header = f"Subject: {subject or ''}\nFrom: {from_addr or ''}"

    body = ""
    if not is_truncated:
        if body_text:
            body = body_text
        elif body_html:
            body = _strip_html(body_html)

    body = body.strip()[:content_chars]
    content_level = CONTENT_LEVEL_FULL if body else CONTENT_LEVEL_ENVELOPE
    text = f"{header}\n{body}" if body else header

    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return EmbeddingInput(text=text, content_level=content_level, source_hash=source_hash)
