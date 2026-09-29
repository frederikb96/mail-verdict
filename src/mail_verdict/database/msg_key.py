"""
The durable message key: (account_id, msg_key) identifies a message across
PostIMAP row-id churn -- a UIDVALIDITY change or a folder rename on a server
without persistent ids replaces every messages.id in a folder, and the RFC
5322 Message-ID header is what survives that.

Every owned table that persists something about a specific message keys on
this rather than on messages.id.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import GlacierMessage, Message

# Separates the fields going into the hash fallback so that, e.g., an empty
# subject concatenated with a one-character sender can never collide with a
# non-empty subject and no sender.
_FIELD_SEPARATOR = "\x1f"


def compute_msg_key(
    *,
    account_id: uuid.UUID,
    message_id_hdr: str | None,
    from_addr: str | None,
    subject: str | None,
    received_at: datetime | None,
    size_bytes: int | None,
) -> str:
    """
    Compute the durable key for a message.

    The RFC 5322 Message-ID header (with its angle brackets, matching what
    PostIMAP stores) when present. A message with no such header -- rare,
    but not exceptional enough to skip the durability gate it would
    otherwise fall through -- gets a hash of envelope fields that are
    themselves stable across a resync.

    Args:
        account_id: Account the message belongs to, scoping the key so two
            accounts can never collide on the same header or envelope
        message_id_hdr: RFC Message-ID header value, or None if absent
        from_addr: Envelope sender
        subject: Message subject
        received_at: Message receipt timestamp
        size_bytes: Message size in bytes

    Returns:
        The header itself when present, otherwise `sha256:<hex digest>`
    """
    if message_id_hdr:
        return message_id_hdr

    parts = (
        str(account_id),
        from_addr or "",
        subject or "",
        received_at.isoformat() if received_at is not None else "",
        str(size_bytes) if size_bytes is not None else "",
    )
    digest = hashlib.sha256(_FIELD_SEPARATOR.join(parts).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class ResolvedMessage:
    """Wherever a message durably identified by (account_id, msg_key)
    lives right now."""

    kind: Literal["live", "glacier"]
    id: uuid.UUID
    folder_id: uuid.UUID


async def resolve_by_msg_key(
    session: AsyncSession, *, account_id: uuid.UUID, msg_key: str,
) -> ResolvedMessage | None:
    """
    One resolver from the durable (account_id, msg_key) identity to
    wherever the message it names lives right now -- a live row, a
    visible glacier row, or neither (never mirrored under this key, or
    gone for good). Every caller that needs "the current copy of this
    message" (an id a client held before a move or a glacier round trip,
    a durable reference another feature stores) goes through this
    rather than re-deriving the same two-table lookup at each call site.

    The common case -- a real Message-ID header -- is msg_key verbatim
    (compute_msg_key's own doc), which matches messages.message_id
    directly since the live table stores the header, not a computed key.
    The hash-fallback form (no header at all) has no column on the live
    side to match against without recomputing the hash per candidate
    row, so it only ever resolves through glacier_messages' own stored
    msg_key column -- a message with no header that has never been
    glaciered cannot be found by this function at all, only by its own
    live id.

    Args:
        session: Active AsyncSession
        account_id: Scopes the key the same way it scopes compute_msg_key
        msg_key: The durable identity, as compute_msg_key produces it

    Returns:
        None if nothing currently answers to this identity
    """
    if not msg_key.startswith("sha256:"):
        live = (
            await session.execute(
                select(Message.id, Message.folder_id)
                .where(
                    Message.account_id == account_id, Message.message_id == msg_key,
                    Message.expunged_at.is_(None),
                )
                .order_by(desc(Message.created_at))
                .limit(1)
            )
        ).one_or_none()
        if live is not None:
            return ResolvedMessage(kind="live", id=live.id, folder_id=live.folder_id)

    glacier = (
        await session.execute(
            select(GlacierMessage.id, GlacierMessage.folder_id).where(
                GlacierMessage.account_id == account_id, GlacierMessage.msg_key == msg_key,
                GlacierMessage.visible_at.is_not(None),
            )
        )
    ).one_or_none()
    if glacier is not None:
        return ResolvedMessage(kind="glacier", id=glacier.id, folder_id=glacier.folder_id)
    return None
