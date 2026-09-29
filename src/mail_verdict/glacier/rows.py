"""
Column-compatible read access to the glacier: the small helpers every
listing, search or unified-view query that must span both `messages`
and `glacier_messages` builds its union on top of (database/models.py's
GlacierMessage docstring explains why this is a query-time union rather
than a database VIEW).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import AccountPrefs, GlacierMessage


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
    """A Select over glacier_messages, visible rows only, shaped exactly
    like select(Message) so a caller can union it with one and read the
    result through an aliased Message the way
    database/repository.py:_build_candidate_query already does for its
    own two-branch union. Callers add their own account/folder/date/seen
    filters on top."""
    return select(GlacierMessage).where(GlacierMessage.visible_at.is_not(None))


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
