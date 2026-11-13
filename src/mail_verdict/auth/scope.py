"""
What a caller may see and touch -- the one access check.

Every read or write of a user's data goes through a Scope: the user it acts
for, whether that user is admin, and the mail and DAV accounts they own.
Request code gets one from api/deps.py's current_scope() (a FastAPI
dependency, also awaited directly by MCP tools and the SSE endpoint);
background code builds one for the user it acts for with Scope.for_user()
or Scope.for_account_owner().

The rules a caller follows:

- A list over a table keyed by account filters with
  `scope.account_filter(Table.account_id)` (`dav_account_filter` for DAV);
  a table keyed by user filters on `Table.user_id == scope.user_id`.
- A read or write by id loads the row first, then calls
  `scope.require_account(row.account_id)`: a missing row is 404, a row of
  someone else's is 403.
- An account id the client names (a query parameter, a body field, an MCP
  argument) is never trusted: `scope.narrow(account_id)` intersects it with
  what the caller owns, or `require_account` refuses it outright.
- Admin is not "every account". An admin's scope covers the admin's own
  accounts, exactly like anyone else's; reaching other users' metadata is
  the /api/admin routes' job, gated by `require_admin()`, and none of them
  read mail.

A filter never means "no filter": an empty scope (Scope.system(), a user
with no accounts) matches nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import ColumnElement, Select, literal, or_, select
from sqlalchemy.orm import InstrumentedAttribute

from mail_verdict.auth.principal import OWNER_USER_ID, Principal
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import AccountOwner, DavAccountOwner, User

# The event-ring key admin-only events go to (see ring_keys()).
ADMIN_RING_KEY = "admin"
# The event-ring key every stream receives: only events that carry nothing
# of anyone's (a resync, a bare "this category changed") go here.
EVERYONE_RING_KEY = "everyone"


class ScopeDenied(HTTPException):
    """Something the scope does not cover. A 403 for the client; inside an
    MCP tool, the same HTTPException its callers already translate."""

    def __init__(self, detail: str = "forbidden") -> None:
        super().__init__(status_code=403, detail=detail)


@dataclass(frozen=True)
class Scope:
    """
    The user a piece of work acts for, and what that user owns.

    Attributes:
        user_id: The user, or None for Scope.system().
        is_admin: Whether the admin routes and admin settings are open.
        account_ids: Mail accounts the user owns.
        dav_account_ids: CalDAV/CardDAV accounts the user owns.
    """

    user_id: uuid.UUID | None
    is_admin: bool
    account_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    dav_account_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)

    @property
    def is_system(self) -> bool:
        """Whether this scope acts for nobody (see Scope.system())."""
        return self.user_id is None

    def owns_account(self, account_id: uuid.UUID | None) -> bool:
        """Whether a mail account is the caller's own."""
        return account_id is not None and account_id in self.account_ids

    def owns_dav_account(self, dav_account_id: uuid.UUID | None) -> bool:
        """Whether a DAV account is the caller's own."""
        return dav_account_id is not None and dav_account_id in self.dav_account_ids

    def require_account(self, account_id: uuid.UUID | None) -> None:
        """Refuse (403) a mail account that is not the caller's own."""
        if not self.owns_account(account_id):
            raise ScopeDenied()

    def require_dav_account(self, dav_account_id: uuid.UUID | None) -> None:
        """Refuse (403) a DAV account that is not the caller's own."""
        if not self.owns_dav_account(dav_account_id):
            raise ScopeDenied()

    def require_admin(self) -> None:
        """Refuse (403) a caller who is not admin."""
        if not self.is_admin:
            raise ScopeDenied("admin_only")

    def require_user(self) -> uuid.UUID:
        """The user id, for writing a row that belongs to the caller.
        Refuses (403) Scope.system(), which belongs to nobody."""
        if self.user_id is None:
            raise ScopeDenied("no_user")
        return self.user_id

    def account_filter(
        self, column: InstrumentedAttribute[uuid.UUID] | ColumnElement[uuid.UUID],
    ) -> ColumnElement[bool]:
        """A WHERE clause keeping rows of the caller's mail accounts."""
        return column.in_(self.account_ids)

    def dav_account_filter(
        self, column: InstrumentedAttribute[uuid.UUID] | ColumnElement[uuid.UUID],
    ) -> ColumnElement[bool]:
        """A WHERE clause keeping rows of the caller's DAV accounts."""
        return column.in_(self.dav_account_ids)

    def narrow(self, account_id: uuid.UUID | None) -> frozenset[uuid.UUID]:
        """The accounts a client-named filter leaves: every account of the
        caller's for None, that one account if it is theirs, else none."""
        if account_id is None:
            return self.account_ids
        return frozenset({account_id}) & self.account_ids

    def ring_keys(self) -> frozenset[str]:
        """The live-update keys this scope may receive: each owned account
        and DAV account, the user's own key, the admin key for admins, and
        the key every stream receives."""
        keys = {str(a) for a in self.account_ids} | {str(d) for d in self.dav_account_ids}
        keys.add(EVERYONE_RING_KEY)
        if self.user_id is not None:
            keys.add(str(self.user_id))
        if self.is_admin:
            keys.add(ADMIN_RING_KEY)
        return frozenset(keys)

    @classmethod
    def system(cls) -> Scope:
        """A scope that acts for nobody and owns nothing: every filter built
        from it matches no row, every require_* refuses. For work that
        belongs to no user; work done for a user takes that user's scope."""
        return cls(user_id=None, is_admin=False)

    @classmethod
    async def for_principal(cls, db: DatabaseConnection, principal: Principal) -> Scope:
        """The scope of a signed-in request."""
        account_ids, dav_account_ids = await _owned(db, principal.user_id)
        return cls(
            user_id=principal.user_id,
            is_admin=principal.is_admin,
            account_ids=account_ids,
            dav_account_ids=dav_account_ids,
        )

    @classmethod
    async def for_user(cls, db: DatabaseConnection, user_id: uuid.UUID) -> Scope:
        """The scope of a user, for background work done on their behalf.
        A user that does not exist gets an empty scope, never a wider one."""
        async with db.session() as session:
            is_admin = bool(await session.scalar(
                select(_is_admin_user()).select_from(User).where(User.id == user_id)
            ))
        account_ids, dav_account_ids = await _owned(db, user_id)
        return cls(
            user_id=user_id,
            is_admin=is_admin or user_id == OWNER_USER_ID,
            account_ids=account_ids,
            dav_account_ids=dav_account_ids,
        )

    @classmethod
    async def for_account_owner(cls, db: DatabaseConnection, account_id: uuid.UUID) -> Scope:
        """The scope of whoever owns a mail account -- what background work
        triggered by that account's mail acts as. An account with no owner
        gets Scope.system()."""
        async with db.session() as session:
            owner = await session.scalar(
                select(AccountOwner.user_id).where(AccountOwner.account_id == account_id)
            )
        return cls.system() if owner is None else await cls.for_user(db, owner)

    @classmethod
    async def for_dav_account_owner(
        cls, db: DatabaseConnection, dav_account_id: uuid.UUID,
    ) -> Scope:
        """for_account_owner, for a DAV account."""
        async with db.session() as session:
            owner = await session.scalar(
                select(DavAccountOwner.user_id).where(
                    DavAccountOwner.dav_account_id == dav_account_id
                )
            )
        return cls.system() if owner is None else await cls.for_user(db, owner)


def _is_admin_user() -> ColumnElement[bool]:
    """SQL: a users row is an admin -- by role, or as the owner row."""
    return or_(User.role == "admin", User.id == OWNER_USER_ID)


def admin_user_ids() -> Select[uuid.UUID]:
    """The ids of every enabled admin: who receives what belongs to no user
    (an alert about the instance itself)."""
    return select(User.id).where(_is_admin_user(), User.disabled.is_(False))


async def _owned(
    db: DatabaseConnection, user_id: uuid.UUID,
) -> tuple[frozenset[uuid.UUID], frozenset[uuid.UUID]]:
    """The mail and DAV accounts a user owns, in one round trip."""
    async with db.session() as session:
        rows = (
            await session.execute(
                select(AccountOwner.account_id, literal(True).label("mail"))
                .where(AccountOwner.user_id == user_id)
                .union_all(
                    select(
                        DavAccountOwner.dav_account_id, literal(False).label("mail"),
                    ).where(DavAccountOwner.user_id == user_id)
                )
            )
        ).all()
    return (
        frozenset(row[0] for row in rows if row[1]),
        frozenset(row[0] for row in rows if not row[1]),
    )
