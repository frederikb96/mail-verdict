"""
A named, persisted circuit breaker.

Not owned by any one queue: two queues that happen to call the same
provider share one breaker by sharing its name, which is the whole reason
this is a name-keyed row rather than a field on queue_state. What triggers
each transition -- a 429, a run of 5xx, a 401 -- is provider vocabulary
that belongs to whichever caller wraps that provider's client, not to this
module.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mail_verdict.database.models import CircuitBreakerState

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)


class CircuitState(str, enum.Enum):
    """A circuit breaker's persisted state."""

    CLOSED = "closed"
    OPEN = "open"
    SUSPENDED = "suspended"


@dataclass(frozen=True)
class CircuitStatus:
    """A snapshot of one circuit breaker's row."""

    name: str
    state: CircuitState
    reason: str | None
    since: datetime | None
    retry_after: datetime | None


class CircuitBreaker:
    """A named health gate backed by circuit_breakers, one row per name."""

    def __init__(self, db: DatabaseConnection, name: str) -> None:
        """
        Args:
            db: Database connection the breaker's row lives behind
            name: Arbitrary identifier -- a provider name shared across
                queues, or a queue name, entirely the caller's choice
        """
        self._db = db
        self._name = name

    async def status(self) -> CircuitStatus:
        """
        Read the breaker's current row, creating it closed if it doesn't
        exist yet -- so a breaker nobody has tripped reads exactly like one
        that was explicitly closed, with no separate "unknown" state to
        handle at every call site.

        Returns:
            The breaker's current status
        """
        async with self._db.session() as session:
            stmt = pg_insert(CircuitBreakerState).values(name=self._name)
            stmt = stmt.on_conflict_do_nothing(index_elements=["name"])
            await session.execute(stmt)
            result = await session.execute(
                text(
                    "SELECT state, reason, since, retry_after FROM circuit_breakers "
                    "WHERE name = :name"
                ),
                {"name": self._name},
            )
            row = result.one()
            return CircuitStatus(
                name=self._name,
                state=CircuitState(row.state),
                reason=row.reason,
                since=row.since,
                retry_after=row.retry_after,
            )

    async def is_available(self) -> bool:
        """
        Whether ordinary traffic should proceed right now.

        Closed is always available. Open self-clears once its cooldown has
        elapsed -- no explicit action needed, the next caller's success is
        what actually closes it via `record_success`. Suspended is never
        available for ordinary traffic; only `try_probe` may call through.

        Returns:
            True if a caller should proceed with normal work
        """
        status = await self.status()
        if status.state == CircuitState.CLOSED:
            return True
        if status.state == CircuitState.OPEN:
            return status.retry_after is not None and _now() >= status.retry_after
        return False

    async def record_success(self) -> None:
        """Close the breaker -- a call succeeded, whatever state it was in."""
        async with self._db.session() as session:
            await session.execute(
                text(
                    "UPDATE circuit_breakers SET state = 'closed', reason = NULL, "
                    "since = NULL, retry_after = NULL WHERE name = :name"
                ),
                {"name": self._name},
            )

    async def record_backoff(self, *, retry_after: timedelta, reason: str | None = None) -> None:
        """
        Open the breaker for a bounded, self-clearing duration -- a rate
        limit or a run of transient errors, not a credentials problem.

        Args:
            retry_after: How long until ordinary traffic may resume
            reason: Recorded for the observability surface
        """
        async with self._db.session() as session:
            await session.execute(
                text(
                    "INSERT INTO circuit_breakers (name, state, reason, since, retry_after) "
                    "VALUES (:name, 'open', :reason, now(), now() + :retry_after) "
                    "ON CONFLICT (name) DO UPDATE SET "
                    "state = 'open', reason = EXCLUDED.reason, "
                    "since = CASE WHEN circuit_breakers.state = 'closed' "
                    "THEN now() ELSE circuit_breakers.since END, "
                    "retry_after = EXCLUDED.retry_after"
                ),
                {"name": self._name, "reason": reason, "retry_after": retry_after},
            )

    async def record_unavailable(self, *, reason: str, probe_interval: timedelta) -> None:
        """
        Suspend the breaker -- credentials rejected outright, or no key
        configured. Requires an explicit successful probe to clear, never
        just the passage of time.

        Args:
            reason: Recorded for the observability surface
            probe_interval: Minimum gap between probe attempts
        """
        async with self._db.session() as session:
            await session.execute(
                text(
                    "INSERT INTO circuit_breakers (name, state, reason, since, retry_after) "
                    "VALUES (:name, 'suspended', :reason, now(), now() + :probe_interval) "
                    "ON CONFLICT (name) DO UPDATE SET "
                    "state = 'suspended', reason = EXCLUDED.reason, "
                    "since = CASE WHEN circuit_breakers.state != 'suspended' "
                    "THEN now() ELSE circuit_breakers.since END, "
                    "retry_after = EXCLUDED.retry_after"
                ),
                {"name": self._name, "reason": reason, "probe_interval": probe_interval},
            )
        logger.warning(
            "Circuit breaker suspended", extra={"breaker": self._name, "reason": reason},
        )

    async def try_probe(self, *, probe_interval: timedelta) -> bool:
        """
        Atomically claim the right to run the next probe call.

        Guarded so that when several workers idle against a suspended
        breaker, at most one of them fires a probe per interval rather than
        every worker hammering the still-revoked credential at once.

        Args:
            probe_interval: How long this claim blocks the next one

        Returns:
            True if this caller won the right to probe now
        """
        async with self._db.session() as session:
            result = await session.execute(
                text(
                    "UPDATE circuit_breakers SET retry_after = now() + :probe_interval "
                    "WHERE name = :name AND state = 'suspended' AND retry_after <= now()"
                ),
                {"name": self._name, "probe_interval": probe_interval},
            )
            return (result.rowcount or 0) > 0  # type: ignore[attr-defined]


def _now() -> datetime:
    """Current UTC time."""
    return datetime.now(timezone.utc)
