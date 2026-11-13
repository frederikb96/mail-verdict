"""
Who is calling: the principal a validated token resolves to.

A token's subject resolves to a user through user_identities. The subjects in
auth.owner_subjects resolve to OWNER_USER_ID, as admin, from configuration
alone -- no table is read for them, so the owner can always sign in, whatever
state the users tables are in. Any other subject resolves through the
database; an unknown one becomes a new ordinary user when auth.auto_provision
allows it.

The resolved principal is bound for the request by auth/middleware.py and read
back with current_principal(). Code acting on data does not read it directly:
it asks for a Scope (auth/scope.py), which carries the accounts the principal
owns.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mail_verdict.database.connection import DatabaseConnection, get_db_connection
from mail_verdict.database.models import User, UserIdentity

logger = logging.getLogger(__name__)

# The user every account that predates users belongs to, created by the 0050
# migration; auth.owner_subjects sign in as it.
OWNER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

class AuthError(Exception):
    """A request that cannot proceed as anyone. `status` and `detail` are
    what the client is told."""

    status = 401
    detail = "not_authenticated"


class UserDisabled(AuthError):
    """The user exists and is switched off."""

    status = 403
    detail = "user_disabled"


class UnknownUser(AuthError):
    """A valid token for a subject no user has, with auto-provisioning off."""

    status = 403
    detail = "unknown_user"


@dataclass(frozen=True)
class Principal:
    """The user a request acts as.

    Attributes:
        user_id: The users row.
        subject: The token subject that signed in.
        is_admin: Whether this user may use the admin routes and settings.
    """

    user_id: uuid.UUID
    subject: str
    is_admin: bool


_current_principal: ContextVar[Principal | None] = ContextVar(
    "mail_verdict_principal", default=None,
)


@contextmanager
def bind_principal(principal: Principal) -> Iterator[None]:
    """Make `principal` the current one for the code run inside this block."""
    token = _current_principal.set(principal)
    try:
        yield
    finally:
        _current_principal.reset(token)


def current_principal() -> Principal | None:
    """
    The principal of the request being handled, or None outside one.

    MCP tool calls see the principal of the HTTP request that carried the
    call, not of the request that opened the MCP session --
    tests/unit/test_auth_middleware.py holds FastMCP to that.
    """
    return _current_principal.get()


@dataclass(frozen=True)
class _DirectoryEntry:
    """What the user directory knows about a subject."""

    user_id: uuid.UUID
    role: str
    disabled: bool


class UserDirectory(Protocol):
    """The reads and writes PrincipalResolver needs from the users tables."""

    async def find(self, subject: str) -> _DirectoryEntry | None:
        """The user a subject signs in as, or None."""

    async def provision(
        self, subject: str, email: str | None, display_name: str,
    ) -> _DirectoryEntry:
        """Create an ordinary user for a new subject (or return the one a
        concurrent request just created)."""

    async def touch(self, user_id: uuid.UUID, email: str | None) -> None:
        """Record that the user was seen, and their email if none is stored."""


class DatabaseUserDirectory:
    """UserDirectory over the users and user_identities tables, through the
    application's database connection (looked up per call, since the gate is
    built before the lifespan opens it)."""

    @property
    def _db(self) -> DatabaseConnection:
        return get_db_connection()

    async def find(self, subject: str) -> _DirectoryEntry | None:
        """The user a subject signs in as, or None."""
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(User.id, User.role, User.disabled)
                    .join(UserIdentity, UserIdentity.user_id == User.id)
                    .where(UserIdentity.subject == subject)
                )
            ).one_or_none()
        if row is None:
            return None
        return _DirectoryEntry(user_id=row.id, role=row.role, disabled=row.disabled)

    async def provision(
        self, subject: str, email: str | None, display_name: str,
    ) -> _DirectoryEntry:
        """Create a user and its identity in one transaction. A concurrent
        first request for the same subject loses the identity insert and is
        answered with the winner's user; its own user row is rolled back."""
        user_id = uuid.uuid4()
        async with self._db.session() as session:
            session.add(User(id=user_id, email=email, display_name=display_name, role="user"))
            await session.flush()
            inserted = await session.scalar(
                pg_insert(UserIdentity)
                .values(subject=subject, user_id=user_id)
                .on_conflict_do_nothing(index_elements=[UserIdentity.subject])
                .returning(UserIdentity.user_id)
            )
            if inserted is None:
                await session.rollback()
        if inserted is None:
            existing = await self.find(subject)
            if existing is None:
                raise AuthError("identity vanished during provisioning")
            return existing
        logger.info("Provisioned user %s for a new subject", user_id)
        return _DirectoryEntry(user_id=user_id, role="user", disabled=False)

    async def touch(self, user_id: uuid.UUID, email: str | None) -> None:
        """Record that the user was seen, and their email if none is stored."""
        values: dict[str, Any] = {"last_seen_at": datetime.now(timezone.utc)}
        async with self._db.session() as session:
            await session.execute(update(User).where(User.id == user_id).values(**values))
            if email:
                await session.execute(
                    update(User)
                    .where(User.id == user_id, User.email.is_(None))
                    .values(email=email)
                )


def display_name_from_claims(claims: Mapping[str, Any]) -> str:
    """What to call a new user: their name, else their login, else their
    email, else the bare subject."""
    for claim in ("name", "preferred_username", "email", "sub"):
        value = claims.get(claim)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "user"


class PrincipalResolver:
    """
    Turns validated token claims into a Principal.

    Results for subjects that go through the database are remembered for
    `cache_seconds`, so a disabled user or a role change takes effect within
    that window; `forget()` drops them at once on the replica that changed one.
    """

    def __init__(
        self,
        directory: UserDirectory,
        owner_subjects: list[str],
        *,
        auto_provision: bool,
        cache_seconds: float,
    ) -> None:
        self._directory = directory
        self._owner_subjects = frozenset(owner_subjects)
        self._auto_provision = auto_provision
        self._cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, Principal | AuthError]] = {}

    async def resolve(self, claims: Mapping[str, Any]) -> Principal:
        """
        The principal for a validated token's claims.

        Raises:
            UserDisabled: The subject's user is disabled.
            UnknownUser: The subject is unknown and auto-provisioning is off.
        """
        subject = str(claims["sub"])
        if subject in self._owner_subjects:
            return Principal(user_id=OWNER_USER_ID, subject=subject, is_admin=True)

        cached = self._cache.get(subject)
        if cached is not None and cached[0] > time.monotonic():
            outcome = cached[1]
        else:
            outcome = await self._lookup(subject, claims)
            self._cache[subject] = (time.monotonic() + self._cache_seconds, outcome)
        if isinstance(outcome, AuthError):
            raise outcome
        return outcome

    def forget(self, user_id: uuid.UUID | None = None) -> None:
        """Drop remembered results -- one user's, or every one."""
        if user_id is None:
            self._cache.clear()
            return
        for subject, (_, outcome) in list(self._cache.items()):
            if isinstance(outcome, Principal) and outcome.user_id == user_id:
                del self._cache[subject]
        # A disabled user is remembered as an error that names no user id.
        self._cache = {
            subject: entry for subject, entry in self._cache.items()
            if not isinstance(entry[1], AuthError)
        }

    async def _lookup(self, subject: str, claims: Mapping[str, Any]) -> Principal | AuthError:
        """Resolve a subject through the directory, provisioning if allowed."""
        email = claims.get("email") if isinstance(claims.get("email"), str) else None
        entry = await self._directory.find(subject)
        if entry is None:
            if not self._auto_provision:
                return UnknownUser()
            entry = await self._directory.provision(
                subject, email, display_name_from_claims(claims),
            )
        if entry.disabled and entry.user_id != OWNER_USER_ID:
            return UserDisabled()
        await self._directory.touch(entry.user_id, email)
        return Principal(
            user_id=entry.user_id,
            subject=subject,
            is_admin=entry.role == "admin" or entry.user_id == OWNER_USER_ID,
        )


_resolver: PrincipalResolver | None = None


def init_principal_resolver(resolver: PrincipalResolver) -> PrincipalResolver:
    """Install the application's resolver (server.py, once per app)."""
    global _resolver
    _resolver = resolver
    return resolver


def get_principal_resolver() -> PrincipalResolver | None:
    """The application's resolver, for code that changes a user and must
    drop what it remembers about them (forget())."""
    return _resolver
