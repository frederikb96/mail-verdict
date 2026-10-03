"""
Resolving where each of an order's mails is now -- the detail endpoint's
own re-check, done fresh on every read rather than trusted from the
stored message_id hint.

Membership is (account_id, msg_key), never messages.id, but the stored
message_id is still tried first as a join hint -- the same tie-break
api/mails.py::locate_message uses: a move through this API keeps a live
row's id, so most reads resolve on that one direct lookup, and only fall
through to a fresh (account_id, msg_key) resolution when the hint is
stale (the row moved under another client, or moved into or out of the
glacier, which assigns an entirely new id). database/msg_key.py's
resolve_by_msg_key is the one resolver for that fallback -- both the
live-by-header case and the glacier case -- so there is no second lookup
for either; this module is only the id-hint fast path in front of it and
the is_seen follow-up resolve_by_msg_key's minimal return doesn't carry.
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
    if mail.message_id is not None:
        row = (
            await session.execute(
                select(Message.id, Message.folder_id, Message.is_seen).where(
                    Message.id == mail.message_id, Message.account_id == mail.account_id,
                    Message.expunged_at.is_(None),
                )
            )
        ).one_or_none()
        if row is not None:
            return ResolvedMail(
                order_mail_id=mail.id, location="mailbox", message_id=row.id,
                folder_id=row.folder_id, is_seen=row.is_seen,
            )

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
