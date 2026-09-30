"""
Resolving where each of an order's mails is now -- the detail endpoint's
own re-check, done fresh on every read rather than trusted from the
stored message_id hint.

Membership is (account_id, msg_key), never messages.id: a move through
this API keeps the id, but a move made by another IMAP client is mirrored
as an expunge in the source folder plus a new row sharing only the
Message-ID header, and a move into or out of the glacier assigns an
entirely new id. database/msg_key.py's resolve_by_msg_key is the one
resolver from that durable identity to wherever the mail lives right now
(a live row, a glacier row, or neither); this module is only the is_seen
follow-up that resolver's minimal return doesn't carry, never a second
lookup.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import GlacierMessage, Message, OrderMail
from mail_verdict.database.msg_key import resolve_by_msg_key

Location = Literal["mailbox", "glacier", "gone"]


@dataclass(frozen=True)
class ResolvedMail:
    """Where one order_mails row's mail is now."""

    order_mail_id: uuid.UUID
    location: Location
    message_id: uuid.UUID | None
    folder_id: uuid.UUID | None
    is_seen: bool | None


async def _resolve_one(session: AsyncSession, mail: OrderMail) -> ResolvedMail:
    resolved = await resolve_by_msg_key(session, account_id=mail.account_id, msg_key=mail.msg_key)
    if resolved is None:
        return ResolvedMail(
            order_mail_id=mail.id, location="gone", message_id=None,
            folder_id=None, is_seen=None,
        )

    if resolved.kind == "live":
        is_seen = (
            await session.execute(select(Message.is_seen).where(Message.id == resolved.id))
        ).scalar_one()
        location: Location = "mailbox"
    else:
        is_seen = (
            await session.execute(
                select(GlacierMessage.is_seen).where(GlacierMessage.id == resolved.id)
            )
        ).scalar_one()
        location = "glacier"

    return ResolvedMail(
        order_mail_id=mail.id, location=location, message_id=resolved.id,
        folder_id=resolved.folder_id, is_seen=is_seen,
    )


async def resolve_mails(
    session: AsyncSession, mails: list[OrderMail],
) -> dict[uuid.UUID, ResolvedMail]:
    """
    Resolve every mail of an order to where it is now.

    Args:
        session: Session to read through
        mails: The order's order_mails rows

    Returns:
        Mapping of order_mails.id to its resolution
    """
    return {mail.id: await _resolve_one(session, mail) for mail in mails}
