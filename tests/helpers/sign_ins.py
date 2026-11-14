"""An in-memory sign_ins table, for unit tests of the gate."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from mail_verdict.auth.sign_ins import Presented, Recorded, SignInRegistry, refuses


@dataclass
class Row:
    """One stored sign-in."""

    id: uuid.UUID
    user_id: uuid.UUID
    kind: str
    handle: str
    subject: str
    name: str | None
    last_seen_at: datetime
    revoked_at: datetime | None = None


class InMemorySignInStore:
    """SignInStore over a dict, with the database store's semantics."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], Row] = {}
        self.writes = 0

    async def record(
        self, presented: Presented, user_id: uuid.UUID, subject: str, name: str | None,
    ) -> Recorded:
        """See SignInStore.record."""
        self.writes += 1
        now = datetime.now(timezone.utc)
        key = (presented.kind, presented.handle)
        row = self.rows.get(key)
        if row is None:
            row = Row(uuid.uuid4(), user_id, presented.kind, presented.handle, subject, name, now)
            self.rows[key] = row
            return Recorded(row.id, user_id, name, None)
        if refuses(presented, row.revoked_at):
            return Recorded(row.id, row.user_id, row.name, row.revoked_at)
        row.user_id, row.subject, row.last_seen_at, row.revoked_at = user_id, subject, now, None
        if name is not None:
            row.name = name
        return Recorded(row.id, user_id, row.name, None)

    def revoke(self, kind: str, handle: str, at: datetime | None = None) -> None:
        """Revoke one stored sign-in, now or at `at`."""
        self.rows[(kind, handle)].revoked_at = at or datetime.now(timezone.utc)


def registry(
    store: InMemorySignInStore, *, native_client_id: str = "native-app", cache_seconds: float = 0,
) -> SignInRegistry:
    """A registry over `store` that remembers nothing unless told to."""
    return SignInRegistry(
        store, native_client_id=native_client_id, native_handle="zitadel_jti",
        cache_seconds=cache_seconds,
    )
