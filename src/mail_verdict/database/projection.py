"""
The narrow projection search results are displayed from.

A message row carries its whole body and raw source -- tens of kilobytes,
130 KB for a mail with attachments -- while a search result row shows a
sender, a subject, a date and a few flags. Loading display rows through this
projection, for live and glaciered messages alike, keeps a result page's
cost in step with what it shows.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, false, select, true, union_all

from mail_verdict.database.models import GlacierMessage, Message

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Message columns a result row is built from; GlacierMessage carries the
# same names (tests/unit/test_glacier_columns.py).
DISPLAY_COLUMNS: tuple[str, ...] = (
    "id", "account_id", "folder_id", "thread_id", "subject", "from_addr", "to_addrs",
    "received_at", "is_seen", "is_flagged", "is_answered", "is_draft", "imap_uid",
    "is_truncated", "created_at",
)


@dataclass(frozen=True)
class MessageDisplay:
    """One message as a search result shows it."""

    id: uuid.UUID
    account_id: uuid.UUID
    folder_id: uuid.UUID
    thread_id: uuid.UUID
    subject: str | None
    from_addr: str | None
    to_addrs: Any | None
    received_at: datetime | None
    is_seen: bool
    is_flagged: bool
    is_answered: bool
    is_draft: bool
    imap_uid: int | None
    is_truncated: bool
    created_at: datetime
    is_glacier: bool


def display_statement(ids: Sequence[uuid.UUID]) -> Select[Any]:
    """The live and the visible glaciered messages among `ids`, as
    DISPLAY_COLUMNS plus whether the row came from the glacier."""
    live = select(
        *[Message.__table__.c[name] for name in DISPLAY_COLUMNS], false().label("is_glacier"),
    ).where(Message.id.in_(ids))
    glacier = select(
        *[GlacierMessage.__table__.c[name] for name in DISPLAY_COLUMNS],
        true().label("is_glacier"),
    ).where(GlacierMessage.id.in_(ids), GlacierMessage.visible_at.is_not(None))
    return union_all(live, glacier)  # type: ignore[return-value]


async def load_display(
    session: AsyncSession, ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, MessageDisplay]:
    """Display rows for these message ids, keyed by id; ids that resolve to
    nothing are absent."""
    if not ids:
        return {}
    rows = (await session.execute(display_statement(ids))).all()
    return {row.id: MessageDisplay(**row._mapping) for row in rows}
