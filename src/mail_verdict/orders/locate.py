"""
Resolving where each of an order's mails is now -- the detail endpoint's
own re-check, done fresh on every read rather than trusted from the
stored message_id hint.

Membership is (account_id, msg_key), never messages.id: a move through
this API keeps the id, but a move made by another IMAP client is mirrored
as an expunge in the source folder plus a new row sharing only the
Message-ID header (api/mails.py::locate_message does the same tie-break
this module uses). Glacier storage adds its own lookup here once merged;
until then, a mail neither live nor glaciered reads "gone".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import Message, OrderMail

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
    row = None
    if mail.message_id is not None:
        result = await session.execute(
            select(Message.id, Message.folder_id, Message.is_seen).where(
                Message.id == mail.message_id, Message.account_id == mail.account_id,
                Message.expunged_at.is_(None),
            )
        )
        row = result.one_or_none()

    if row is None and not mail.msg_key.startswith("sha256:"):
        result = await session.execute(
            select(Message.id, Message.folder_id, Message.is_seen)
            .where(
                Message.account_id == mail.account_id, Message.message_id == mail.msg_key,
                Message.expunged_at.is_(None),
            )
            .order_by(Message.imap_uid.is_(None), desc(Message.created_at))
            .limit(1)
        )
        row = result.one_or_none()

    if row is not None:
        return ResolvedMail(
            order_mail_id=mail.id, location="mailbox", message_id=row.id,
            folder_id=row.folder_id, is_seen=row.is_seen,
        )

    glaciered = await _resolve_glacier(session, mail)
    if glaciered is not None:
        return glaciered

    return ResolvedMail(
        order_mail_id=mail.id, location="gone", message_id=None,
        folder_id=None, is_seen=None,
    )


async def _resolve_glacier(session: AsyncSession, mail: OrderMail) -> ResolvedMail | None:
    """Glacier's own lookup by (account_id, msg_key) -- a stub until the
    glacier feature is merged, at which point this reads its table and
    returns location="glacier" for a mail that has been moved there."""
    return None


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
