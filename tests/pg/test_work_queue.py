"""
WorkQueue mechanics against a throwaway table -- claim, lease, heartbeat,
and the terminal transitions. Nothing here knows about messages,
embeddings or verdicts; that's the point.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Table, text

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.queue.work_queue import REQUIRED_COLUMNS, WorkQueue


async def _seed_rows(db: DatabaseConnection, table: Table, count: int) -> list[uuid.UUID]:
    """Insert `count` pending rows, return their ids."""
    ids = [uuid.uuid4() for _ in range(count)]
    async with db.session() as session:
        for row_id in ids:
            await session.execute(
                text(f"INSERT INTO {table.name} (id, priority) VALUES (:id, 100)"),
                {"id": row_id},
            )
    return ids


async def _row(db: DatabaseConnection, table: Table, row_id: uuid.UUID) -> object:
    async with db.session() as session:
        result = await session.execute(
            text(f"SELECT * FROM {table.name} WHERE id = :id"), {"id": row_id},
        )
        return result.one()


class TestTableValidation:
    """WorkQueue refuses a table missing a required column at construction."""

    def test_missing_column_raises_at_construction(self, migrated_db: DatabaseConnection) -> None:
        from sqlalchemy import Column, MetaData, Uuid

        incomplete = Table("incomplete", MetaData(), Column("id", Uuid, primary_key=True))
        with pytest.raises(ValueError, match="missing columns"):
            WorkQueue(migrated_db, incomplete)

    def test_every_required_column_is_checked(self, migrated_db: DatabaseConnection) -> None:
        from sqlalchemy import Column, MetaData, Text, Uuid

        for missing in sorted(REQUIRED_COLUMNS):
            present = [
                Column("id", Uuid, primary_key=True) if name == "id" else Column(name, Text)
                for name in sorted(REQUIRED_COLUMNS - {missing})
            ]
            table = Table(f"partial_{missing}", MetaData(), *present)
            with pytest.raises(ValueError, match=missing):
                WorkQueue(migrated_db, table)


class TestClaimBatch:
    @pytest.mark.asyncio
    async def test_claims_up_to_batch_size(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """More pending rows than batch_size -- only batch_size are claimed."""
        await _seed_rows(migrated_db, throwaway_queue_table, 5)
        queue = WorkQueue(migrated_db, throwaway_queue_table)

        claimed = await queue.claim_batch(worker_id="w1", batch_size=3, lease_seconds=30)

        assert len(claimed) == 3
        assert {row["status"] for row in claimed} == {"claimed"}
        assert {row["claimed_by"] for row in claimed} == {"w1"}

    @pytest.mark.asyncio
    async def test_claim_increments_attempts(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """Attempts count against the row at claim time, not at failure -- a row
        that crashes its worker every single time still exhausts its attempts."""
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        queue = WorkQueue(migrated_db, throwaway_queue_table)

        claimed = await queue.claim_batch(worker_id="w1", batch_size=1, lease_seconds=30)

        assert claimed[0]["id"] == row_id
        assert claimed[0]["attempts"] == 1

    @pytest.mark.asyncio
    async def test_future_next_attempt_at_is_not_claimable(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """A row backed off into the future is invisible to a claim until it's due."""
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        async with migrated_db.session() as session:
            await session.execute(
                text(
                    f"UPDATE {throwaway_queue_table.name} "
                    "SET next_attempt_at = now() + interval '1 hour' WHERE id = :id"
                ),
                {"id": row_id},
            )
        queue = WorkQueue(migrated_db, throwaway_queue_table)

        claimed = await queue.claim_batch(worker_id="w1", batch_size=10, lease_seconds=30)

        assert claimed == []

    @pytest.mark.asyncio
    async def test_two_workers_never_claim_the_same_row(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """FOR UPDATE SKIP LOCKED under concurrent claims -- every row goes to
        exactly one worker, and together they claim everything available."""
        ids = await _seed_rows(migrated_db, throwaway_queue_table, 40)
        queue = WorkQueue(migrated_db, throwaway_queue_table)

        results = await asyncio.gather(
            *(
                queue.claim_batch(worker_id=f"worker-{i}", batch_size=25, lease_seconds=30)
                for i in range(4)
            )
        )

        claimed_ids = [row["id"] for batch in results for row in batch]
        assert sorted(claimed_ids) == sorted(ids)
        assert len(claimed_ids) == len(set(claimed_ids))

    @pytest.mark.asyncio
    async def test_max_attempts_stops_a_row_that_never_reaches_its_own_fail_path(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """attempts alone does not stop reclaim from handing a row back out
        forever: the ordinary retry-vs-fail cap only runs once a claimed
        row reaches the caller's own handler, and a row that instead
        crashes the worker process itself, every time, never gets there --
        it is reclaimed on lease expiry (which deliberately never touches
        attempts) and claimed again with nothing to stop it. max_attempts
        at claim time is what closes that, independently of whether the
        caller's own handler ever runs."""
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        queue = WorkQueue(migrated_db, throwaway_queue_table)

        # Simulate five prior claims that each crashed the worker before
        # ever reaching a retry/fail decision: reclaim_expired would have
        # put the row back to 'pending' each time, attempts intact.
        async with migrated_db.session() as session:
            await session.execute(
                text(f"UPDATE {throwaway_queue_table.name} SET attempts = 5 WHERE id = :id"),
                {"id": row_id},
            )

        claimed = await queue.claim_batch(
            worker_id="w1", batch_size=10, lease_seconds=30, max_attempts=5,
        )

        assert claimed == []

    @pytest.mark.asyncio
    async def test_max_attempts_still_allows_a_row_below_the_cap(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        async with migrated_db.session() as session:
            await session.execute(
                text(f"UPDATE {throwaway_queue_table.name} SET attempts = 4 WHERE id = :id"),
                {"id": row_id},
            )
        queue = WorkQueue(migrated_db, throwaway_queue_table)

        claimed = await queue.claim_batch(
            worker_id="w1", batch_size=10, lease_seconds=30, max_attempts=5,
        )

        assert [row["id"] for row in claimed] == [row_id]

    @pytest.mark.asyncio
    async def test_no_max_attempts_preserves_the_unbounded_default(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """Omitting max_attempts must not newly exclude anything -- every
        existing caller that does not pass it keeps today's behavior."""
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        async with migrated_db.session() as session:
            await session.execute(
                text(f"UPDATE {throwaway_queue_table.name} SET attempts = 999 WHERE id = :id"),
                {"id": row_id},
            )
        queue = WorkQueue(migrated_db, throwaway_queue_table)

        claimed = await queue.claim_batch(worker_id="w1", batch_size=10, lease_seconds=30)

        assert [row["id"] for row in claimed] == [row_id]


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_extends_the_lease_for_the_owning_worker(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        queue = WorkQueue(migrated_db, throwaway_queue_table)
        claimed = await queue.claim_batch(worker_id="w1", batch_size=1, lease_seconds=1)
        first_lease = claimed[0]["lease_expires_at"]

        extended = await queue.heartbeat([row_id], worker_id="w1", lease_seconds=300)

        assert extended == 1
        row = await _row(migrated_db, throwaway_queue_table, row_id)
        assert row.lease_expires_at > first_lease

    @pytest.mark.asyncio
    async def test_wrong_worker_extends_nothing(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """A worker whose lease already expired and got reclaimed must not be able
        to extend a claim it no longer holds."""
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        queue = WorkQueue(migrated_db, throwaway_queue_table)
        await queue.claim_batch(worker_id="w1", batch_size=1, lease_seconds=30)

        extended = await queue.heartbeat([row_id], worker_id="someone-else", lease_seconds=300)

        assert extended == 0


class TestTerminalTransitions:
    @pytest.mark.asyncio
    async def test_complete_releases_the_claim(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        queue = WorkQueue(migrated_db, throwaway_queue_table)
        await queue.claim_batch(worker_id="w1", batch_size=1, lease_seconds=30)

        ok = await queue.complete(row_id, worker_id="w1", status="done")

        assert ok is True
        row = await _row(migrated_db, throwaway_queue_table, row_id)
        assert row.status == "done"
        assert row.claimed_by is None
        assert row.lease_expires_at is None

    @pytest.mark.asyncio
    async def test_complete_by_the_wrong_worker_is_rejected(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        queue = WorkQueue(migrated_db, throwaway_queue_table)
        await queue.claim_batch(worker_id="w1", batch_size=1, lease_seconds=30)

        ok = await queue.complete(row_id, worker_id="w2", status="done")

        assert ok is False
        row = await _row(migrated_db, throwaway_queue_table, row_id)
        assert row.status == "claimed"

    @pytest.mark.asyncio
    async def test_fail_records_the_error(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        queue = WorkQueue(migrated_db, throwaway_queue_table)
        await queue.claim_batch(worker_id="w1", batch_size=1, lease_seconds=30)

        ok = await queue.fail(row_id, worker_id="w1", last_error="schema validation exhausted")

        assert ok is True
        row = await _row(migrated_db, throwaway_queue_table, row_id)
        assert row.status == "failed"
        assert row.last_error == "schema validation exhausted"

    @pytest.mark.asyncio
    async def test_retry_keeps_the_attempt_counted(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """The ordinary transient-failure path: the claim-time increment stays,
        distinguishing it from release_untouched's refund."""
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        queue = WorkQueue(migrated_db, throwaway_queue_table)
        await queue.claim_batch(worker_id="w1", batch_size=1, lease_seconds=30)
        next_attempt = datetime.now(timezone.utc) + timedelta(seconds=5)

        ok = await queue.retry(
            row_id, worker_id="w1", next_attempt_at=next_attempt, last_error="timeout",
        )

        assert ok is True
        row = await _row(migrated_db, throwaway_queue_table, row_id)
        assert row.status == "pending"
        assert row.attempts == 1
        assert row.last_error == "timeout"

    @pytest.mark.asyncio
    async def test_release_untouched_refunds_the_attempt(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """A provider-level fault (rate limited, outage, suspended credentials) is
        not the item's fault -- its attempt count must not move."""
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        queue = WorkQueue(migrated_db, throwaway_queue_table)
        await queue.claim_batch(worker_id="w1", batch_size=1, lease_seconds=30)

        ok = await queue.release_untouched(row_id, worker_id="w1")

        assert ok is True
        row = await _row(migrated_db, throwaway_queue_table, row_id)
        assert row.status == "pending"
        assert row.attempts == 0

    @pytest.mark.asyncio
    async def test_release_all_claims_refunds_every_row(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """A clean shutdown: everything a worker still holds goes back to pending
        with its attempt refunded, in one call rather than per row."""
        ids = await _seed_rows(migrated_db, throwaway_queue_table, 3)
        queue = WorkQueue(migrated_db, throwaway_queue_table)
        await queue.claim_batch(worker_id="w1", batch_size=3, lease_seconds=30)

        released = await queue.release_all_claims(worker_id="w1")

        assert released == 3
        for row_id in ids:
            row = await _row(migrated_db, throwaway_queue_table, row_id)
            assert row.status == "pending"
            assert row.attempts == 0

    @pytest.mark.asyncio
    async def test_poison_pill_exhausts_attempts_rather_than_looping_forever(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """A row that kills its worker on every attempt still reaches a permanent
        failure after max_attempts, instead of being claimed forever."""
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        queue = WorkQueue(migrated_db, throwaway_queue_table)
        max_attempts = 3

        for _ in range(max_attempts):
            claimed = await queue.claim_batch(
                worker_id="crash-worker", batch_size=1, lease_seconds=30,
            )
            assert len(claimed) == 1
            # The worker "crashes" here without completing, failing, or retrying --
            # exactly what a poison pill does. The row sits claimed until reclaimed.
            reclaimed = await queue.reclaim_expired()
            assert reclaimed == 0  # lease hasn't expired yet
            async with migrated_db.session() as session:
                await session.execute(
                    text(
                        f"UPDATE {throwaway_queue_table.name} "
                        "SET lease_expires_at = now() - interval '1 second' WHERE id = :id"
                    ),
                    {"id": row_id},
                )
            reclaimed = await queue.reclaim_expired()
            assert reclaimed == 1

        row = await _row(migrated_db, throwaway_queue_table, row_id)
        assert row.status == "pending"
        assert row.attempts == max_attempts

        # A real worker loop checks attempts against max_attempts after the claim
        # that reaches it and fails permanently instead of retrying again.
        claimed = await queue.claim_batch(worker_id="w-final", batch_size=1, lease_seconds=30)
        assert claimed[0]["attempts"] == max_attempts + 1
        ok = await queue.fail(row_id, worker_id="w-final", last_error="max attempts exceeded")
        assert ok is True


class TestReclaimExpired:
    @pytest.mark.asyncio
    async def test_reclaims_a_dead_workers_row_after_its_lease(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        queue = WorkQueue(migrated_db, throwaway_queue_table)
        await queue.claim_batch(worker_id="dead-worker", batch_size=1, lease_seconds=30)
        async with migrated_db.session() as session:
            await session.execute(
                text(
                    f"UPDATE {throwaway_queue_table.name} "
                    "SET lease_expires_at = now() - interval '1 second' WHERE id = :id"
                ),
                {"id": row_id},
            )

        reclaimed = await queue.reclaim_expired()

        assert reclaimed == 1
        row = await _row(migrated_db, throwaway_queue_table, row_id)
        assert row.status == "pending"
        assert row.claimed_by is None
        # Attempts stays at 1 -- the row was already charged when claimed;
        # reclaim is not a refund path.
        assert row.attempts == 1

    @pytest.mark.asyncio
    async def test_does_not_touch_a_row_still_within_its_lease(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        queue = WorkQueue(migrated_db, throwaway_queue_table)
        await queue.claim_batch(worker_id="w1", batch_size=1, lease_seconds=300)

        reclaimed = await queue.reclaim_expired()

        assert reclaimed == 0
        row = await _row(migrated_db, throwaway_queue_table, row_id)
        assert row.status == "claimed"


class TestCountsByStatus:
    @pytest.mark.asyncio
    async def test_counts_reflect_current_status(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        ids = await _seed_rows(migrated_db, throwaway_queue_table, 3)
        queue = WorkQueue(migrated_db, throwaway_queue_table)
        await queue.claim_batch(worker_id="w1", batch_size=1, lease_seconds=30)
        await queue.complete(ids[0], worker_id="w1", status="done")

        counts = await queue.counts_by_status()

        assert counts["done"] == 1
        assert counts["pending"] == 2
        assert "failed" not in counts
