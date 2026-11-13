"""
Which user owns which account, and how an account's name carries it.

account_owners and dav_account_owners are the authority on ownership. A new
account's owner row is written in the same transaction as the account itself,
so PostIMAP's notification for it never reaches an account without one.

PostIMAP keeps an account's `name` unique across the whole instance, so two
users could not both call an account "Work". The stored name is therefore
`<owner user id>/<name the user chose>`: unique across users by
construction, and unique within one user's accounts by their own choice.
account_display_name() is what every response shows. A name stored without
that prefix (an account that predates users) is shown as it is.

Every change of ownership written here bumps ownership_generation() once its
transaction commits, and calls each on_ownership_change() listener: an open
live-update stream holds the accounts its user owned when it connected, and
this is what tells it to look again (api/events.py). A change to a user's
role or sign-in (user_changed_on_commit) bumps it too, since it changes what
that user's stream may read, or whether it may stay open at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy import delete, event, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import AccountOwner, DavAccountOwner

_generation = 0
_listeners: list[Callable[[], None]] = []
# session.info key marking a session whose commit is already hooked.
_HOOKED = "ownership_changed_on_commit"


def ownership_generation() -> int:
    """A number that changes whenever any account's owner, or any user's role
    or sign-in, has changed."""
    return _generation


def on_ownership_change(listener: Callable[[], None]) -> None:
    """Call `listener` after every committed change of ownership."""
    _listeners.append(listener)


def _changed_on_commit(session: AsyncSession) -> None:
    """Bump the generation once this session's transaction commits -- not
    before: whoever looks again must read the new owner, not the old."""
    if session.info.get(_HOOKED):
        return
    session.info[_HOOKED] = True

    def _bump(_session: object) -> None:
        global _generation
        session.info.pop(_HOOKED, None)
        _generation += 1
        for listener in _listeners:
            listener()

    event.listen(session.sync_session, "after_commit", _bump, once=True)


def user_changed_on_commit(session: AsyncSession) -> None:
    """Bump the generation once this session's change to a user's role or
    sign-in commits, so every open stream reads its user again."""
    _changed_on_commit(session)


def owned_account_name(user_id: uuid.UUID, display_name: str) -> str:
    """The name to store for an account `user_id` calls `display_name`."""
    return f"{user_id}/{display_name}"


def account_display_name(stored_name: str) -> str:
    """The name a user gave an account, from the name PostIMAP stores."""
    prefix, separator, rest = stored_name.partition("/")
    if not separator:
        return stored_name
    try:
        uuid.UUID(prefix)
    except ValueError:
        return stored_name
    return rest


async def owner_of_account(session: AsyncSession, account_id: uuid.UUID) -> uuid.UUID | None:
    """The user owning a mail account, or None if it has no owner row."""
    owner: uuid.UUID | None = await session.scalar(
        select(AccountOwner.user_id).where(AccountOwner.account_id == account_id)
    )
    return owner


async def owner_of_dav_account(
    session: AsyncSession, dav_account_id: uuid.UUID,
) -> uuid.UUID | None:
    """The user owning a DAV account, or None if it has no owner row."""
    owner: uuid.UUID | None = await session.scalar(
        select(DavAccountOwner.user_id).where(DavAccountOwner.dav_account_id == dav_account_id)
    )
    return owner


async def set_account_owner(
    session: AsyncSession, account_id: uuid.UUID, user_id: uuid.UUID,
) -> None:
    """Make `user_id` the owner of a mail account, in the caller's transaction."""
    _changed_on_commit(session)
    await session.execute(
        pg_insert(AccountOwner)
        .values(account_id=account_id, user_id=user_id)
        .on_conflict_do_update(index_elements=[AccountOwner.account_id], set_={"user_id": user_id})
    )


async def set_dav_account_owner(
    session: AsyncSession, dav_account_id: uuid.UUID, user_id: uuid.UUID,
) -> None:
    """Make `user_id` the owner of a DAV account, in the caller's transaction."""
    _changed_on_commit(session)
    await session.execute(
        pg_insert(DavAccountOwner)
        .values(dav_account_id=dav_account_id, user_id=user_id)
        .on_conflict_do_update(
            index_elements=[DavAccountOwner.dav_account_id], set_={"user_id": user_id},
        )
    )


async def forget_account_owner(session: AsyncSession, account_id: uuid.UUID) -> None:
    """Drop a deleted mail account's owner row, in the caller's transaction."""
    _changed_on_commit(session)
    await session.execute(delete(AccountOwner).where(AccountOwner.account_id == account_id))


async def forget_dav_account_owner(session: AsyncSession, dav_account_id: uuid.UUID) -> None:
    """Drop a deleted DAV account's owner row, in the caller's transaction."""
    _changed_on_commit(session)
    await session.execute(
        delete(DavAccountOwner).where(DavAccountOwner.dav_account_id == dav_account_id)
    )
