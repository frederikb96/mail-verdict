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
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import AccountOwner, DavAccountOwner


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
    await session.execute(
        pg_insert(AccountOwner)
        .values(account_id=account_id, user_id=user_id)
        .on_conflict_do_update(index_elements=[AccountOwner.account_id], set_={"user_id": user_id})
    )


async def set_dav_account_owner(
    session: AsyncSession, dav_account_id: uuid.UUID, user_id: uuid.UUID,
) -> None:
    """Make `user_id` the owner of a DAV account, in the caller's transaction."""
    await session.execute(
        pg_insert(DavAccountOwner)
        .values(dav_account_id=dav_account_id, user_id=user_id)
        .on_conflict_do_update(
            index_elements=[DavAccountOwner.dav_account_id], set_={"user_id": user_id},
        )
    )


async def forget_account_owner(session: AsyncSession, account_id: uuid.UUID) -> None:
    """Drop a deleted mail account's owner row, in the caller's transaction."""
    await session.execute(delete(AccountOwner).where(AccountOwner.account_id == account_id))


async def forget_dav_account_owner(session: AsyncSession, dav_account_id: uuid.UUID) -> None:
    """Drop a deleted DAV account's owner row, in the caller's transaction."""
    await session.execute(
        delete(DavAccountOwner).where(DavAccountOwner.dav_account_id == dav_account_id)
    )
