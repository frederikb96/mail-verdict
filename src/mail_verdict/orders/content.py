"""
Body preparation for the orders pipeline: one function, used identically
by the first filter (orders/filter.py), the decide call and the write
call (orders/prompts.py) -- so "what the filter matched against" and
"what the model read" are never two different texts.

A DHL notice's first 1,800 characters are language-switcher URLs; after
dropping link targets that shrinks to a few hundred characters of actual
content (observed on real mail during design). Link targets are dropped
from the cleaned text, with one exception: a shipment number a tracking
link carries in its query string is rendered into the text (some carrier
templates leave the link text an unfilled placeholder, so the target is the
only place the number exists). `prepare_body` also returns the raw,
untouched text, which candidate retrieval's number search scans.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime

import nh3
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import Attachment, Message
from mail_verdict.orders.candidates import normalize_identifier, shipment_number_in_url

# Below this many characters, body_text is treated as too thin to be the
# real content (a plain-text stub next to an HTML-only message) and
# body_html is used instead if there is one.
_MIN_TEXT_LENGTH = 200

_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)]*)\)")
# A carrier template's unfilled merge field, e.g. {DELIVERY_PARCEL_IDENTCODE}.
_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z0-9_]+\}")
_BRACKETED_URL_RE = re.compile(r"\[https?://[^\]]*\]", re.IGNORECASE)
_ANGLE_URL_RE = re.compile(r"<https?://[^>]*>", re.IGNORECASE)
_BARE_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_IMAGE_MARKER_RE = re.compile(r"\[(image|bild|img)[^\]]*\]", re.IGNORECASE)

# Ordinary spaces/tabs plus the invisible characters real mail carries:
# non-breaking space, zero-width space/non-joiner, soft hyphen, BOM.
_WHITESPACE_RUN_RE = re.compile(r"[ \t ​‌­﻿]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class PreparedBody:
    """The cleaned text a filter and a model read, plus the untouched raw
    text a number search scans (a tracking number sometimes lives only
    inside a link's target, which the cleaned text drops)."""

    text: str
    raw: str


def _render_link(match: re.Match[str]) -> str:
    """A markdown link as plain text: its text, unless the target carries a
    shipment number -- then that number replaces an empty or placeholder
    text and is appended to any other text that does not already show it."""
    text, url = match.group(1), match.group(2)
    number = shipment_number_in_url(url)
    if number is None:
        return text
    stripped = text.strip()
    if not stripped or _PLACEHOLDER_RE.fullmatch(stripped):
        return number
    if normalize_identifier(number) in normalize_identifier(stripped):
        return text
    return f"{text} ({number})"


def prepare_body(*, body_text: str | None, body_html: str | None) -> PreparedBody:
    """
    Build the text the first filter, the decide call and the write call
    all read.

    Args:
        body_text: The message's plain-text body, if any
        body_html: The message's HTML body, if any

    Returns:
        PreparedBody with the cleaned text and the raw source
    """
    raw = (body_text or "") + (body_html or "")
    source = body_text or ""
    if len(source) < _MIN_TEXT_LENGTH and body_html:
        source = nh3.clean(body_html, tags=set())

    cleaned = _MD_LINK_RE.sub(_render_link, source)
    cleaned = _BRACKETED_URL_RE.sub("", cleaned)
    cleaned = _ANGLE_URL_RE.sub("", cleaned)
    cleaned = _BARE_URL_RE.sub("", cleaned)
    cleaned = _IMAGE_MARKER_RE.sub("", cleaned)

    cleaned = _WHITESPACE_RUN_RE.sub(" ", cleaned)
    lines = [line.strip() for line in cleaned.split("\n")]
    cleaned = "\n".join(lines)
    cleaned = _MULTI_NEWLINE_RE.sub("\n\n", cleaned).strip()

    return PreparedBody(text=cleaned, raw=raw)


@dataclass(frozen=True)
class OrderMailContent:
    """One mail's content, as loaded for a decide or write call -- an
    explicit column list, never raw_source and never an attachment's own
    data (the same OOM-under-concurrency guard pipeline/message_view.py's
    module docstring explains)."""

    message_id: uuid.UUID
    thread_id: uuid.UUID
    subject: str
    from_addr: str
    to_addrs: str
    received_at: datetime | None
    body: PreparedBody
    attachments: tuple[tuple[str, str], ...]


async def load_order_mail(
    session: AsyncSession, *, account_id: uuid.UUID, msg_key: str, message_id: uuid.UUID | None,
) -> OrderMailContent | None:
    """
    Load one mail's content by its durable identity, re-resolving to
    whatever row currently represents it.

    Tries `message_id` (the stored join hint) first, then -- for a
    header-form msg_key -- the live row carrying that Message-ID header
    on this account, the same tie-break api/mails.py's locate_message
    uses (imap_uid IS NULL last, newest created_at first).

    Args:
        session: Session to read through
        account_id: The mail's account
        msg_key: The durable key (database/msg_key.py)
        message_id: The stored join hint, or None

    Returns:
        The content, or None if the mail is gone
    """
    columns = (
        Message.id, Message.thread_id, Message.subject, Message.from_addr,
        Message.to_addrs, Message.received_at, Message.body_text, Message.body_html,
    )

    row = None
    if message_id is not None:
        result = await session.execute(
            select(*columns).where(
                Message.id == message_id, Message.account_id == account_id,
                Message.expunged_at.is_(None),
            )
        )
        row = result.one_or_none()

    if row is None and not msg_key.startswith("sha256:"):
        result = await session.execute(
            select(*columns)
            .where(
                Message.account_id == account_id, Message.message_id == msg_key,
                Message.expunged_at.is_(None),
            )
            .order_by(Message.imap_uid.is_(None), desc(Message.created_at))
            .limit(1)
        )
        row = result.one_or_none()

    if row is None:
        return None

    att_result = await session.execute(
        select(Attachment.filename, Attachment.content_type).where(
            Attachment.message_id == row.id,
        )
    )
    attachments = tuple(
        (name or "", ctype or "") for name, ctype in att_result.all()
    )

    prepared = prepare_body(body_text=row.body_text, body_html=row.body_html)
    to_addrs = row.to_addrs
    to_str = ", ".join(
        addr if isinstance(addr, str) else str(addr.get("address", ""))
        for addr in (to_addrs or [])
    ) if isinstance(to_addrs, list) else ""

    return OrderMailContent(
        message_id=row.id, thread_id=row.thread_id, subject=row.subject or "",
        from_addr=row.from_addr or "", to_addrs=to_str, received_at=row.received_at,
        body=prepared, attachments=attachments,
    )
