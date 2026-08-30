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
from datetime import datetime

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
