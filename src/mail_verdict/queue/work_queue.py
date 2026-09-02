"""
The claim/lease/backoff mechanics shared by every work queue.

The work table *is* the queue: there is no separate jobs table to
disagree with the domain rows it queues. This module knows only about the
columns below -- never a message, an embedding, a verdict or a stage. Any
table with these columns can be handed to WorkQueue, which is what lets
message_embeddings and pipeline_runs share this code unchanged: they only
need to agree on names, not on a shared base class or a shared migration.

    id                uuid PRIMARY KEY
    status             text          -- this module only ever writes 'pending' / 'claimed'
    priority           smallint/int
    next_attempt_at    timestamptz
    created_at         timestamptz
    claimed_by         text, nullable
    claimed_at         timestamptz, nullable
    lease_expires_at   timestamptz, nullable
    attempts           integer
    last_error         text, nullable

Every terminal status name (done, skipped, failed, cancelled, ...) is the
caller's vocabulary, passed as a plain string -- this module never asserts
what a row means, only where it is in the claim/lease lifecycle.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Table, text

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = frozenset({
    "id", "status", "priority", "next_attempt_at", "created_at",
    "claimed_by", "claimed_at", "lease_expires_at", "attempts", "last_error",
})


def _require_columns(table: Table) -> None:
    """Fail fast, at registration time, if a table is missing a column this
    engine writes to -- rather than at the first claim, with a database
    error naming a column instead of a queue."""
    missing = REQUIRED_COLUMNS - set(table.columns.keys())
    if missing:
        raise ValueError(
            f"table '{table.name}' is missing columns required by WorkQueue: "
            f"{sorted(missing)}"
        )


class WorkQueue:
    """Claim, lease, and backoff mechanics over one Postgres table.

    The table name is taken from a SQLAlchemy Table object at construction,
    never from a caller-supplied string at call time -- every SQL statement
    below interpolates it directly, and this is what keeps that safe: it can
    only ever be one of the identifiers this process itself defined.
    """

    def __init__(self, db: DatabaseConnection, table: Table) -> None:
        """
        Args:
            db: Database connection to run claim/lease statements against
            table: The table this queue claims rows from; validated to
                carry every column this engine touches
        """
        _require_columns(table)
        self._db = db
        self._table_name = table.name

    async def claim_batch(
        self, *, worker_id: str, batch_size: int, lease_seconds: float,
        max_attempts: int | None = None,
    ) -> list[Mapping[str, Any]]:
        """
        Claim up to `batch_size` pending, due rows, skipping locked ones.

        Increments `attempts` on every claimed row -- deliberately, so that
        a row that kills its worker every time still exhausts its attempts
        rather than looping forever; a provider-level fault that should not
        count against an item's own attempts is refunded via
        `release_untouched` rather than never incremented here.

        Args:
            worker_id: Identifier recorded as the current claimant
            batch_size: Maximum rows to claim in this call
            lease_seconds: How long this worker holds the claim before a
                reconciliation pass may reclaim it
            max_attempts: When given, a row already at or past this many
                attempts is never claimed again. `attempts` alone does not
                stop a row from being reclaimed forever: the ordinary
                retry-vs-fail decision is the caller's own, checked only
                once a claimed row actually reaches that code -- a row
                that instead crashes the worker process itself, every
                time, before ever getting there is reclaimed on lease
                expiry (`reclaim_expired` deliberately leaves `attempts`
                alone) and claimed again with nothing to stop it. This is
                the one place attempts are checked against a cap for a
                row that never gets the chance to fail itself.

        Returns:
            One mapping per claimed row, with every column of the table
        """
        async with self._db.session() as session:
            result = await session.execute(
                text(
                    f"""
                    WITH candidate AS (
                        SELECT id FROM {self._table_name}
                        WHERE status = 'pending' AND next_attempt_at <= now()
                          AND (
                              CAST(:max_attempts AS integer) IS NULL
                              OR attempts < CAST(:max_attempts AS integer)
                          )
                        ORDER BY priority, next_attempt_at, created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT :batch_size
                    )
                    UPDATE {self._table_name} t
                    SET status = 'claimed', claimed_by = :worker_id, claimed_at = now(),
                        lease_expires_at = now() + make_interval(secs => :lease_seconds),
                        attempts = attempts + 1
                    FROM candidate c
                    WHERE t.id = c.id
                    RETURNING t.*
                    """
                ),
                {
                    "batch_size": batch_size, "worker_id": worker_id,
                    "lease_seconds": lease_seconds, "max_attempts": max_attempts,
                },
            )
            rows: list[Mapping[str, Any]] = [dict(row._mapping) for row in result.all()]
            return rows

    async def heartbeat(
        self, ids: Sequence[UUID], *, worker_id: str, lease_seconds: float,
    ) -> int:
        """
        Extend the lease on rows this worker still holds.

        Args:
            ids: Rows to extend
            worker_id: Must match the row's current claimant, or the
                extension is silently skipped for that row -- it was
                already reclaimed by someone else
            lease_seconds: New lease duration from now

        Returns:
            Number of rows actually extended
        """
        if not ids:
            return 0
        async with self._db.session() as session:
            result = await session.execute(
                text(
                    f"""
                    UPDATE {self._table_name}
                    SET lease_expires_at = now() + make_interval(secs => :lease_seconds)
                    WHERE id = ANY(:ids) AND status = 'claimed' AND claimed_by = :worker_id
                    """
                ),
                {"ids": list(ids), "worker_id": worker_id, "lease_seconds": lease_seconds},
            )
            return result.rowcount or 0  # type: ignore[attr-defined]

    async def complete(self, item_id: UUID, *, worker_id: str, status: str) -> bool:
        """
        Move a claimed row to a terminal status, releasing its claim.

        Args:
            item_id: Row to complete
            worker_id: Must match the row's current claimant
            status: Terminal status name (caller's vocabulary -- 'done',
                'skipped', 'cancelled', ...; not 'failed', see `fail`)

        Returns:
            True if this worker still held the claim and the update landed;
            False means the row was reclaimed from this worker already and
            the caller's result must be discarded, not applied
        """
        async with self._db.session() as session:
            result = await session.execute(
                text(
                    f"""
                    UPDATE {self._table_name}
                    SET status = :status, claimed_by = NULL, claimed_at = NULL,
                        lease_expires_at = NULL
                    WHERE id = :id AND status = 'claimed' AND claimed_by = :worker_id
                    """
                ),
                {"id": item_id, "status": status, "worker_id": worker_id},
            )
            return (result.rowcount or 0) > 0  # type: ignore[attr-defined]

    async def fail(self, item_id: UUID, *, worker_id: str, last_error: str) -> bool:
        """
        Move a claimed row to 'failed' permanently -- attempts exhausted or
        an error the caller has decided is not worth retrying.

        Args:
            item_id: Row to fail
            worker_id: Must match the row's current claimant
            last_error: Recorded on the row for the failure list

        Returns:
            True if this worker still held the claim and the update landed
        """
        async with self._db.session() as session:
            result = await session.execute(
                text(
                    f"""
                    UPDATE {self._table_name}
                    SET status = 'failed', claimed_by = NULL, claimed_at = NULL,
                        lease_expires_at = NULL, last_error = :last_error
                    WHERE id = :id AND status = 'claimed' AND claimed_by = :worker_id
                    """
                ),
                {"id": item_id, "worker_id": worker_id, "last_error": last_error},
            )
            return (result.rowcount or 0) > 0  # type: ignore[attr-defined]

    async def retry(
        self, item_id: UUID, *, worker_id: str, next_attempt_at: datetime, last_error: str | None,
    ) -> bool:
        """
        Return a claimed row to 'pending' for a later attempt.

        The attempt already counted at claim time stays counted -- this is
        the ordinary transient-failure path. A provider-level fault that
        must not count against the item uses `release_untouched` instead.

        Args:
            item_id: Row to requeue
            worker_id: Must match the row's current claimant
            next_attempt_at: When this row becomes claimable again
            last_error: Recorded on the row, or None to leave it as-is

        Returns:
            True if this worker still held the claim and the update landed
        """
        async with self._db.session() as session:
            result = await session.execute(
                text(
                    f"""
                    UPDATE {self._table_name}
                    SET status = 'pending', claimed_by = NULL, claimed_at = NULL,
                        lease_expires_at = NULL, next_attempt_at = :next_attempt_at,
                        last_error = COALESCE(:last_error, last_error)
                    WHERE id = :id AND status = 'claimed' AND claimed_by = :worker_id
                    """
                ),
                {
                    "id": item_id, "worker_id": worker_id,
                    "next_attempt_at": next_attempt_at, "last_error": last_error,
                },
            )
            return (result.rowcount or 0) > 0  # type: ignore[attr-defined]

    async def release_untouched(self, item_id: UUID, *, worker_id: str) -> bool:
        """
        Return a claimed row to 'pending' immediately, refunding the attempt
        claiming it consumed.

        For a provider-level fault (rate limited, outage, suspended
        credentials) -- the item itself did nothing wrong, so it must not
        burn one of its own attempts. Also used for a graceful shutdown,
        where a still-claimed but not-yet-started row is simply given back.

        Args:
            item_id: Row to release
            worker_id: Must match the row's current claimant

        Returns:
            True if this worker still held the claim and the update landed
        """
        async with self._db.session() as session:
            result = await session.execute(
                text(
                    f"""
                    UPDATE {self._table_name}
                    SET status = 'pending', claimed_by = NULL, claimed_at = NULL,
                        lease_expires_at = NULL, attempts = GREATEST(attempts - 1, 0)
                    WHERE id = :id AND status = 'claimed' AND claimed_by = :worker_id
                    """
                ),
                {"id": item_id, "worker_id": worker_id},
            )
            return (result.rowcount or 0) > 0  # type: ignore[attr-defined]

    async def release_all_claims(self, *, worker_id: str) -> int:
        """
        Release every row still claimed by this worker, refunding their
        attempts -- the graceful-shutdown path, called once rather than per
        row so a rolling restart does not wait out a single lease.

        Args:
            worker_id: Worker whose claims to release

        Returns:
            Number of rows released
        """
        async with self._db.session() as session:
            result = await session.execute(
                text(
                    f"""
                    UPDATE {self._table_name}
                    SET status = 'pending', claimed_by = NULL, claimed_at = NULL,
                        lease_expires_at = NULL, attempts = GREATEST(attempts - 1, 0)
                    WHERE status = 'claimed' AND claimed_by = :worker_id
                    """
                ),
                {"worker_id": worker_id},
            )
            return result.rowcount or 0  # type: ignore[attr-defined]

    async def reclaim_expired(self) -> int:
        """
        Return every row whose lease has expired to 'pending', without
        touching `attempts` -- the row was already charged for this attempt
        when it was claimed, and a worker that died mid-item is exactly the
        poison-pill case that increment is meant to catch.

        Meant to run on the reconciliation timer behind an advisory lock,
        never per-worker -- see queue/notify.py's ReconciliationTimer.

        Returns:
            Number of rows reclaimed
        """
        async with self._db.session() as session:
            result = await session.execute(
                text(
                    f"""
                    UPDATE {self._table_name}
                    SET status = 'pending', claimed_by = NULL, claimed_at = NULL,
                        lease_expires_at = NULL
                    WHERE status = 'claimed' AND lease_expires_at < now()
                    """
                )
            )
            reclaimed = result.rowcount or 0  # type: ignore[attr-defined]
            if reclaimed:
                logger.warning(
                    "Reclaimed expired leases",
                    extra={"table": self._table_name, "count": reclaimed},
                )
            return reclaimed

    async def counts_by_status(self) -> dict[str, int]:
        """
        Row counts grouped by status -- the queue depth an observability
        surface reads directly.

        Returns:
            Mapping of status name to row count; a status with zero rows is
            simply absent, not present with a 0
        """
        async with self._db.session() as session:
            result = await session.execute(
                text(f"SELECT status, count(*) AS n FROM {self._table_name} GROUP BY status")
            )
            return {row.status: row.n for row in result.all()}
