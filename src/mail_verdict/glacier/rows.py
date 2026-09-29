"""
Column-compatible read access to the glacier: the small helpers every
listing, search or unified-view query that must span both `messages`
and `glacier_messages` builds its union on top of (database/models.py's
GlacierMessage docstring explains why this is a query-time union rather
than a database VIEW).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import AccountPrefs, GlacierMessage, Message

# Message's own column names, in its table's declared order -- the same
# order select(Message) emits, since a single-table ORM select follows
# the mapped table's own Table.columns. Building the glacier arm's
# column list by indexing GlacierMessage.__table__.c with these exact
# names (rather than by Python attribute name, which need not match --
# "references" is msg_references on both models) is what makes a UNION
# against select(Message) line up column-for-column: tests/unit/
# test_glacier_columns.py is what actually guarantees every name here
# resolves on the glacier side too.
_MESSAGE_COLUMN_NAMES: list[str] = [c.name for c in Message.__table__.columns]


async def glacier_folder_ids(session: AsyncSession) -> dict[uuid.UUID, uuid.UUID]:
    """Every account with an enabled glacier, keyed by its glacier
    folder id -- resolved once per request, not joined per row.

    Args:
        session: Active AsyncSession

    Returns:
        {glacier_folder_id: account_id}
    """
    result = await session.execute(
        select(AccountPrefs.glacier_folder_id, AccountPrefs.account_id).where(
            AccountPrefs.glacier_enabled.is_(True), AccountPrefs.glacier_folder_id.is_not(None),
        )
    )
    return {row.glacier_folder_id: row.account_id for row in result.all()}


def glacier_branch() -> Select[tuple[GlacierMessage]]:
    """A Select over glacier_messages, visible rows only (D8), for a
    caller that wants full GlacierMessage rows -- the manual actions in
    api/mails.py that already resolve a glacier id directly. For a UNION
    against select(Message), use glacier_as_message_select() instead:
    this one carries glacier_messages' own extra columns (msg_key,
    state, ...) and is not column-compatible with Message."""
    return select(GlacierMessage).where(GlacierMessage.visible_at.is_not(None))


def glacier_as_message_select() -> Select[Any]:
    """A Select over glacier_messages, visible rows only (D8), with
    exactly Message's own columns in Message's own order -- what makes
    `select(Message).where(...).union(glacier_as_message_select().where(...))`
    produce a row shape `aliased(Message, the_union.subquery())` can bind
    to, the same pattern database/repository.py:_build_candidate_query
    already uses for its own to_addrs union arm.

    Every predicate a caller adds on top must be written against
    `GlacierMessage.<name>`, never `Message.<name>` -- the two are
    different mapped classes over different tables that merely share
    column names.
    """
    cols = [GlacierMessage.__table__.c[name] for name in _MESSAGE_COLUMN_NAMES]
    return select(*cols).where(GlacierMessage.visible_at.is_not(None))


def scope_touches_glacier(
    folder_ids: set[uuid.UUID] | None, known_glacier_ids: set[uuid.UUID],
) -> bool:
    """Whether a request's folder scope can reach a glacier at all, so a
    caller can skip adding a union branch entirely when it cannot --
    which is what makes an installation with the feature off, or a
    request scoped to real folders only, pay nothing for it.

    Args:
        folder_ids: The request's folder scope, or None for unscoped
            (account-wide or instance-wide)
        known_glacier_ids: Every glacier folder id that currently exists

    Returns:
        True if a union branch over glacier_messages should be added
    """
    if not known_glacier_ids:
        return False
    if folder_ids is None:
        return True
    return bool(folder_ids & known_glacier_ids)
