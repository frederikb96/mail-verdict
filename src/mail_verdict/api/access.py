"""
Ownership checks for routes that name mail by account, message or folder.

auth/scope.py defines what a caller owns; this module answers the question
every mail route has to ask first -- whose is the thing the path names --
so each route states the check in one line instead of re-deriving the
owner:

- `owned_account` is a FastAPI dependency for a `{account_id}` path
  parameter (router-wide on routers whose every path starts with one).
- `ensure_message_access` and `ensure_folder_access` resolve a message or
  folder id to its account and refuse (403) one the caller does not own.
  An id that resolves to nothing passes: the route's own lookup then
  answers 404, exactly as it did before there were users.

A message id may name a live row, a glacier row, or the id a message had
before it was glaciered; a folder id may be a real folder or an account's
synthetic glacier folder. Every shape resolves to the account it belongs to.
"""

from __future__ import annotations

import uuid

from fastapi import Depends
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.deps import current_scope
from mail_verdict.auth.scope import Scope
from mail_verdict.database.models import AccountPrefs, Folder, GlacierMessage, Message


async def owned_account(
    account_id: uuid.UUID, scope: Scope = Depends(current_scope),
) -> uuid.UUID:
    """Dependency: refuse (403) a path whose account is not the caller's own."""
    scope.require_account(account_id)
    return account_id


async def message_account_id(session: AsyncSession, message_id: uuid.UUID) -> uuid.UUID | None:
    """The account a message id belongs to, whichever table holds it -- a
    live row (expunged or not), a glacier row (tombstones included), or the
    pre-glacier id a glacier row remembers. None when nothing has that id."""
    account_id: uuid.UUID | None = await session.scalar(
        select(Message.account_id).where(Message.id == message_id)
    )
    if account_id is not None:
        return account_id
    glacier_account_id: uuid.UUID | None = await session.scalar(
        select(GlacierMessage.account_id)
        .where(
            or_(GlacierMessage.id == message_id, GlacierMessage.origin_message_id == message_id),
        )
        .limit(1)
    )
    return glacier_account_id


async def folder_account_id(session: AsyncSession, folder_id: uuid.UUID) -> uuid.UUID | None:
    """The account a folder id belongs to -- a real folder (a deleted one
    included) or an account's synthetic glacier folder. None when nothing
    has that id."""
    account_id: uuid.UUID | None = await session.scalar(
        select(Folder.account_id).where(Folder.id == folder_id)
    )
    if account_id is not None:
        return account_id
    glacier_account_id: uuid.UUID | None = await session.scalar(
        select(AccountPrefs.account_id).where(AccountPrefs.glacier_folder_id == folder_id)
    )
    return glacier_account_id


async def ensure_message_access(
    session: AsyncSession, scope: Scope, message_id: uuid.UUID,
) -> uuid.UUID | None:
    """Refuse (403) a message of someone else's. Returns its account, or
    None for an id that resolves to nothing (the caller's own 404 follows)."""
    account_id = await message_account_id(session, message_id)
    if account_id is not None:
        scope.require_account(account_id)
    return account_id


async def ensure_folder_access(
    session: AsyncSession, scope: Scope, folder_id: uuid.UUID,
) -> uuid.UUID | None:
    """Refuse (403) a folder of someone else's. Returns its account, or
    None for an id that resolves to nothing (the caller's own 404 follows)."""
    account_id = await folder_account_id(session, folder_id)
    if account_id is not None:
        scope.require_account(account_id)
    return account_id
