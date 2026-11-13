"""
Acting as a user in tests that bypass the authentication gate.

pg-layer tests mount bare routers on a FastAPI app of their own, with no
middleware, and MCP tests call tools in memory; neither has a request carrying
a token. These helpers give such code a caller:

- `act_as(app, scope)` answers every `Depends(current_scope)` of a bare app
  with `scope` (and `require_principal` with a matching principal).
- `as_principal(principal)` binds a principal around code that awaits
  current_scope() itself (MCP tools, the SSE endpoint).
- `owner_scope(...)` / `user_scope(...)` build the Scope to pass.

A Scope is a plain value: a test seeding accounts directly names the ones it
owns, without writing account_owners rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from fastapi import FastAPI
from sqlalchemy import select, text

from mail_verdict.api.deps import current_scope, require_principal
from mail_verdict.auth.principal import OWNER_USER_ID, Principal, bind_principal
from mail_verdict.auth.scope import Scope
from mail_verdict.database.connection import DatabaseConnection, get_db_connection
from mail_verdict.database.models import Account, DavAccount


def owner_scope(
    account_ids: Iterable[uuid.UUID] = (), dav_account_ids: Iterable[uuid.UUID] = (),
) -> Scope:
    """The instance owner (admin), owning the given accounts."""
    return Scope(
        user_id=OWNER_USER_ID, is_admin=True,
        account_ids=frozenset(account_ids), dav_account_ids=frozenset(dav_account_ids),
    )


def user_scope(
    user_id: uuid.UUID,
    account_ids: Iterable[uuid.UUID] = (),
    dav_account_ids: Iterable[uuid.UUID] = (),
    *,
    is_admin: bool = False,
) -> Scope:
    """An ordinary user (admin if asked), owning the given accounts. The
    user row must exist wherever the code under test writes user_id."""
    return Scope(
        user_id=user_id, is_admin=is_admin,
        account_ids=frozenset(account_ids), dav_account_ids=frozenset(dav_account_ids),
    )


def act_as(app: FastAPI, scope: Scope) -> None:
    """Make every request to a bare app act as `scope`."""
    principal = Principal(
        user_id=scope.require_user(), subject=f"test:{scope.user_id}", is_admin=scope.is_admin,
    )
    app.dependency_overrides[current_scope] = lambda: scope
    app.dependency_overrides[require_principal] = lambda: principal


def act_as_owner_of_everything(app: FastAPI, db: DatabaseConnection) -> None:
    """Make every request to a bare app act as the owner of every mail and
    DAV account in the database at the moment of the request -- for tests of
    an endpoint's own behaviour on rows they seed directly, without writing
    ownership rows. Isolation between users is what tests with real
    ownership rows check."""
    principal = Principal(user_id=OWNER_USER_ID, subject="test:owner", is_admin=True)

    async def everything() -> Scope:
        async with db.session() as session:
            accounts = (await session.execute(select(Account.id))).scalars().all()
            dav_accounts = (await session.execute(select(DavAccount.id))).scalars().all()
        return owner_scope(accounts, dav_accounts)

    app.dependency_overrides[current_scope] = everything
    app.dependency_overrides[require_principal] = lambda: principal


async def owner_owns_everything() -> Scope:
    """The instance owner's scope over every mail account in the database
    right now, after giving the owner every account that has no owner row --
    for tests of an endpoint's own behaviour on rows they seed directly.
    Reads the global connection, which the pg layer's `migrated_db` sets."""
    db = get_db_connection()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO account_owners (account_id, user_id) "
                "SELECT id, :owner FROM accounts ON CONFLICT DO NOTHING"
            ),
            {"owner": OWNER_USER_ID},
        )
    return await Scope.for_user(db, OWNER_USER_ID)


@contextmanager
def as_principal(principal: Principal) -> Iterator[None]:
    """Bind `principal` for code that resolves its own scope (current_scope()
    awaited directly). Enter it before an in-memory `fastmcp.Client(mcp)`
    opens, so the server's tasks inherit it."""
    with bind_principal(principal):
        yield
