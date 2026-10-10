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

from sqlalchemy import Select, or_, select
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


def glacier_branch() -> Select[GlacierMessage]:
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


async def touches_glacier(
    session: AsyncSession,
    *,
    account_id: uuid.UUID | None,
    folder_ids: Any | None,
) -> dict[uuid.UUID, uuid.UUID]:
    """Which glacier folders (folder_id -> account_id) a request scoped
    this way can reach -- the one predicate every caller that needs to
    decide "does a union branch over glacier_messages pay for anything
    here" (api/mails.py's listing, database/repository.py's search) goes
    through, so an installation with the feature off, or a request
    scoped to real folders only, never derives that conclusion twice.

    Args:
        session: Active AsyncSession
        account_id: The request's account scope, or None for every account
        folder_ids: The request's folder scope (any sequence/selectable
            `in_()` accepts), or None for unscoped

    Returns:
        Empty when nothing in scope can reach a glacier
    """
    known = await glacier_folder_ids(session)
    if not known:
        return {}
    if folder_ids is not None:
        return {fid: aid for fid, aid in known.items() if fid in folder_ids}
    if account_id is not None:
        return {fid: aid for fid, aid in known.items() if aid == account_id}
    return known


async def glacier_ids_among(
    session: AsyncSession, ids: list[uuid.UUID],
) -> frozenset[uuid.UUID]:
    """Which of these ids are visible glacier rows -- for a caller
    (api/mails.py's listing, api/search.py's results) whose returned
    rows are indistinguishably Message-typed regardless of which table
    they actually came from (aliased(Message, union_subquery) returns
    Message instances for both arms), so imap_uid alone cannot tell a
    glacier row (always NULL, GlacierMessage's own docstring) from a
    live one with a move pending (also NULL)."""
    if not ids:
        return frozenset()
    result = await session.execute(select(GlacierMessage.id).where(GlacierMessage.id.in_(ids)))
    return frozenset(result.scalars())


async def resolve_glacier_id(session: AsyncSession, message_id: uuid.UUID) -> uuid.UUID | None:
    """Whether `message_id` already names a visible glacier row, or is
    the *original* live id a message had before it was glaciered (its
    `messages` row still exists, expunged, and a client that had it
    open -- or a browser tab, a saved link, a reply already drafted --
    still holds that id): one lookup covers both, since a caller
    resolving detail/thread/raw-source/attachments/quote for a message
    cannot tell up front which shape it was handed. A glacier row's
    `origin_message_id` is set to exactly this id at copy time and never
    changes afterward for an ordinary (non-resynced) glacier, which is
    what makes the second half of this safe.

    Visible rows only (D8); a tombstone (already restored) resolves
    through locate_message's own Message-ID-header twin search instead,
    not here.
    """
    result = await session.execute(
        select(GlacierMessage.id)
        .where(
            GlacierMessage.visible_at.is_not(None),
            or_(GlacierMessage.id == message_id, GlacierMessage.origin_message_id == message_id),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def was_permanently_expunged(session: AsyncSession, message_id: uuid.UUID) -> bool:
    """Whether `message_id` -- its own id or the original pre-glacier id
    -- names a glacier row destroyed on purpose through the permanent-
    delete action (`state = 'expunged'`, the tombstone the delete leaves
    behind instead of removing the row outright).

    A read path falling back to the stale `messages` row for an
    ordinary expunge (moved elsewhere by another client, still readable
    until retention purges it) must not do the same for a message
    destroyed deliberately -- this is the check that tells the two
    apart before that fallback ever runs.

    Args:
        session: Active AsyncSession
        message_id: The id to check, either shape

    Returns:
        True if a destroyed tombstone exists under this id
    """
    result = await session.execute(
        select(GlacierMessage.id)
        .where(
            GlacierMessage.state == "expunged",
            or_(GlacierMessage.id == message_id, GlacierMessage.origin_message_id == message_id),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def get_glacier_message_by_key(
    session: AsyncSession, *, account_id: uuid.UUID, msg_key: str,
) -> GlacierMessage | None:
    """A visible glacier row by its durable identity -- what
    embeddings/worker.py resolves against when a queued row's message_id
    hint is NULL (design section 4.7: a glaciered message's embedding
    hint is set to NULL rather than to the glacier row's own id, so the
    worker cannot find its content the way it finds a live message's)."""
    result = await session.execute(
        select(GlacierMessage).where(
            GlacierMessage.account_id == account_id, GlacierMessage.msg_key == msg_key,
            GlacierMessage.visible_at.is_not(None),
        )
    )
    return result.scalar_one_or_none()
