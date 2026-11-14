"""
Which sign-in a request comes from, and whether it may still be used.

The identity provider authenticates; this decides whether one particular
sign-in of an authenticated user is still allowed. Every request past the
token check is matched to a sign_ins row, created on first sight:

- **phone**: a token from the native app's client (`azp` or `client_id`
  equal to auth.native_client_id). With auth.native_sign_in_handle
  `zitadel_jti` the handle is the OIDC session id Zitadel puts in front of
  every access token's jti (`<session>-at_<token>`), which stays the same
  across every refresh of one sign-in and differs for the next sign-in. A
  native token whose jti is not in that shape is refused
  (DeviceUnrecognised) rather than let through unlisted: the shape is
  Zitadel's internal format, and a change to it must show up as phones
  refused, never as phones that can no longer be revoked. With `subject`
  the handle is the token subject, one entry per person.
- **browser**: an ID token, as an authenticating proxy forwards it (one
  carrying `auth_time` or `sid`). One entry per user: the proxy's sessions
  cannot be told apart reliably, so a person's browsers are revoked
  together.
- **machine**: any other token -- an agent's or script's client-credentials
  token. The handle is the subject; a machine client mints a new provider
  session for every token, so nothing narrower stays the same.

A revoked phone or machine handle is refused for good (DeviceRevoked, the
401 every client treats as signed out). A revoked browser entry refuses
every ID token authenticated (`auth_time`, else `iat`) at or before the
revocation; a fresh sign-in at the identity provider -- a password or other
factor checked again, not merely a refreshed token -- reinstates it.

Results are remembered for auth.principal_cache_seconds, which also paces
how often last_seen_at is written; forget() drops them at once on the
replica that revoked something.
"""

from __future__ import annotations

import dataclasses
import logging
import time
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mail_verdict.auth.principal import AuthError, Principal
from mail_verdict.database.connection import DatabaseConnection, get_db_connection
from mail_verdict.database.models import SignIn

logger = logging.getLogger(__name__)

SignInKind = Literal["phone", "browser", "machine"]
NativeHandle = Literal["zitadel_jti", "subject"]

# The header a native app names itself with: UTF-8, at most this many
# characters. Longer names are cut, not refused.
DEVICE_NAME_HEADER = b"x-device-name"
MAX_DEVICE_NAME_LENGTH = 64

# Zitadel's access token id: `<oidc session id>-at_<token id>`.
_ZITADEL_TOKEN_DELIMITER = "-"
_ZITADEL_ACCESS_TOKEN_PREFIX = "at_"


class DeviceRevoked(AuthError):
    """The sign-in this token belongs to has been revoked."""

    status = 401
    detail = "device_revoked"


class DeviceUnrecognised(AuthError):
    """A native app's token that names no sign-in this server can list."""

    status = 401
    detail = "device_unrecognised"


@dataclass(frozen=True)
class Presented:
    """The sign-in a token presents.

    Attributes:
        kind: phone, browser or machine.
        handle: What identifies it among the user's sign-ins.
        authenticated_at: When the person last proved who they are (a
            browser's auth_time), else when the token was issued.
    """

    kind: SignInKind
    handle: str
    authenticated_at: datetime


def zitadel_session(jti: Any) -> str | None:
    """The OIDC session id a Zitadel access token's jti starts with, or None
    when the jti is not in `<session>-at_<token>` shape."""
    if not isinstance(jti, str):
        return None
    parts = jti.split(_ZITADEL_TOKEN_DELIMITER)
    if len(parts) != 2:
        return None
    session, token = parts
    if not session or not token.startswith(_ZITADEL_ACCESS_TOKEN_PREFIX):
        return None
    if len(token) == len(_ZITADEL_ACCESS_TOKEN_PREFIX):
        return None
    return session


def _instant(value: Any) -> datetime | None:
    """A numeric claim as an aware datetime, or None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def identify(
    claims: Mapping[str, Any],
    user_id: uuid.UUID,
    *,
    native_client_id: str,
    native_handle: NativeHandle,
) -> Presented:
    """
    The sign-in a validated token belongs to (see the module docstring).

    Raises:
        DeviceUnrecognised: A native app's token without a usable handle.
    """
    subject = str(claims["sub"])
    issued_at = _instant(claims.get("iat")) or datetime.now(timezone.utc)
    client = claims.get("azp") or claims.get("client_id")
    if native_client_id and client == native_client_id:
        if native_handle == "subject":
            return Presented("phone", subject, issued_at)
        session = zitadel_session(claims.get("jti"))
        if session is None:
            logger.error(
                "Refused a native app token whose jti is not <session>-at_<token>; "
                "its sign-in cannot be listed or revoked (auth.native_sign_in_handle)"
            )
            raise DeviceUnrecognised()
        return Presented("phone", session, issued_at)
    if "auth_time" in claims or "sid" in claims:
        return Presented(
            "browser", str(user_id), _instant(claims.get("auth_time")) or issued_at,
        )
    return Presented("machine", subject, issued_at)


def device_name(raw: bytes | None) -> str | None:
    """The name an X-Device-Name header carries: UTF-8, control characters
    dropped, cut to MAX_DEVICE_NAME_LENGTH. None when there is none."""
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C").strip()
    return text[:MAX_DEVICE_NAME_LENGTH] or None


@dataclass(frozen=True)
class Recorded:
    """What the store knows about a sign-in after recording a use of it."""

    sign_in_id: uuid.UUID
    user_id: uuid.UUID
    name: str | None
    revoked_at: datetime | None


def refuses(presented: Presented, revoked_at: datetime | None) -> bool:
    """Whether a sign-in revoked at `revoked_at` refuses this token."""
    if revoked_at is None:
        return False
    if presented.kind == "browser":
        return presented.authenticated_at <= revoked_at
    return True


class SignInStore(Protocol):
    """The writes SignInRegistry needs from the sign_ins table."""

    async def record(
        self, presented: Presented, user_id: uuid.UUID, subject: str, name: str | None,
    ) -> Recorded:
        """Create the sign-in on first sight; otherwise, unless it refuses
        this token, mark it seen now, as `user_id`'s, under `name` (when one
        is given). A browser entry a fresh sign-in got past is reinstated."""


class DatabaseSignInStore:
    """SignInStore over the sign_ins table, through the application's
    database connection (looked up per call, since the gate is built before
    the lifespan opens it)."""

    @property
    def _db(self) -> DatabaseConnection:
        return get_db_connection()

    async def record(
        self, presented: Presented, user_id: uuid.UUID, subject: str, name: str | None,
    ) -> Recorded:
        """See SignInStore.record."""
        async with self._db.session() as session:
            inserted = await session.scalar(
                pg_insert(SignIn)
                .values(
                    id=uuid.uuid4(), user_id=user_id, kind=presented.kind,
                    handle=presented.handle, subject=subject, name=name,
                )
                .on_conflict_do_nothing(constraint="uq_sign_ins_kind_handle")
                .returning(SignIn.id)
            )
            if inserted is not None:
                return Recorded(inserted, user_id, name, None)
            row = (
                await session.execute(
                    select(SignIn.id, SignIn.user_id, SignIn.name, SignIn.revoked_at).where(
                        SignIn.kind == presented.kind, SignIn.handle == presented.handle,
                    )
                )
            ).one()
            if refuses(presented, row.revoked_at):
                return Recorded(row.id, row.user_id, row.name, row.revoked_at)
            kept_name = name if name is not None else row.name
            await session.execute(
                update(SignIn)
                .where(SignIn.id == row.id)
                .values(
                    user_id=user_id, subject=subject, name=kept_name,
                    last_seen_at=func.now(), revoked_at=None,
                )
            )
        return Recorded(row.id, user_id, kept_name, None)


class SignInRegistry:
    """Matches each request to its sign-in and refuses a revoked one."""

    def __init__(
        self,
        store: SignInStore,
        *,
        native_client_id: str,
        native_handle: NativeHandle,
        cache_seconds: float,
    ) -> None:
        self._store = store
        self._native_client_id = native_client_id
        self._native_handle = native_handle
        self._cache_seconds = cache_seconds
        self._cache: dict[tuple[str, str], tuple[float, Recorded]] = {}

    async def admit(
        self, principal: Principal, claims: Mapping[str, Any], name: str | None,
    ) -> Principal:
        """
        `principal`, carrying the id of the sign-in its token belongs to.

        Raises:
            DeviceRevoked: That sign-in has been revoked.
            DeviceUnrecognised: A native app's token without a usable handle.
        """
        presented = identify(
            claims, principal.user_id,
            native_client_id=self._native_client_id, native_handle=self._native_handle,
        )
        key = (presented.kind, presented.handle)
        cached = self._cache.get(key)
        recorded: Recorded | None = None
        if cached is not None and cached[0] > time.monotonic():
            recorded = cached[1]
            # Anything the row does not already say is written through: a
            # sign-in now someone else's, a new name, or a browser entry a
            # fresh sign-in may reinstate.
            if (
                recorded.user_id != principal.user_id
                or (name is not None and name != recorded.name)
                or (recorded.revoked_at is not None and not refuses(presented, recorded.revoked_at))
            ):
                recorded = None
        if recorded is None:
            recorded = await self._store.record(
                presented, principal.user_id, principal.subject, name,
            )
            self._cache[key] = (time.monotonic() + self._cache_seconds, recorded)
        if refuses(presented, recorded.revoked_at):
            raise DeviceRevoked()
        return dataclasses.replace(principal, sign_in_id=recorded.sign_in_id)

    def forget(self) -> None:
        """Drop everything remembered, so a revocation counts at once."""
        self._cache.clear()


_registry: SignInRegistry | None = None


def init_sign_in_registry(registry: SignInRegistry) -> SignInRegistry:
    """Install the application's registry (server.py, once per app)."""
    global _registry
    _registry = registry
    return registry


def get_sign_in_registry() -> SignInRegistry | None:
    """The application's registry, for code that revokes a sign-in and must
    drop what it remembers (forget())."""
    return _registry
