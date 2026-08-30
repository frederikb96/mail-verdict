"""
default_worker_loop: the canonical claim/process/release-on-stop loop,
proving a clean shutdown releases whatever in a claimed batch was never
handed to the handler. Also heartbeat_while and default_worker_loop's use
of it: a call that runs longer than its lease must have that lease
extended rather than reclaimed out from under the worker still running it.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import Table, text

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.queue.work_queue import WorkQueue
from mail_verdict.queue.worker_loop import default_worker_loop, heartbeat_while


async def _seed_rows(db: DatabaseConnection, table: Table, count: int) -> list[uuid.UUID]:
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


@pytest.mark.asyncio
async def test_a_stop_request_mid_batch_releases_the_rest(
    migrated_db: DatabaseConnection, throwaway_queue_table: Table,
) -> None:
    """A worker asked to stop after handling two of five claimed rows leaves the
    other three pending with their attempt refunded, not stuck claimed until
    their lease expires."""
    ids = await _seed_rows(migrated_db, throwaway_queue_table, 5)
    queue = WorkQueue(migrated_db, throwaway_queue_table)
    stop_event = asyncio.Event()
    handled: list[uuid.UUID] = []

    async def handle_item(row: object) -> None:
        row_id = row["id"]  # type: ignore[index]
        handled.append(row_id)
        if len(handled) == 2:
            stop_event.set()
        await queue.complete(row_id, worker_id="w1", status="done")

    await default_worker_loop(
        queue, worker_id="w1", stop_event=stop_event,
        batch_size=5, lease_seconds=30, handle_item=handle_item,
    )

    assert len(handled) == 2
    counts = await queue.counts_by_status()
    assert counts.get("done", 0) == 2
    assert counts.get("pending", 0) == 3

    async with migrated_db.session() as session:
        result = await session.execute(
            text(f"SELECT id, status, attempts, claimed_by FROM {throwaway_queue_table.name}"),
        )
        rows = {row.id: row for row in result.all()}

    for row_id in ids:
        if row_id in handled:
            assert rows[row_id].status == "done"
        else:
            assert rows[row_id].status == "pending"
            assert rows[row_id].attempts == 0
            assert rows[row_id].claimed_by is None


@pytest.mark.asyncio
async def test_no_work_does_not_busy_loop(
    migrated_db: DatabaseConnection, throwaway_queue_table: Table,
) -> None:
    """An empty queue with the stop_event set before the first claim returns
    immediately rather than claiming or sleeping first."""
    queue = WorkQueue(migrated_db, throwaway_queue_table)
    stop_event = asyncio.Event()
    stop_event.set()
    called = False

    async def handle_item(row: object) -> None:
        nonlocal called
        called = True

    await asyncio.wait_for(
        default_worker_loop(
            queue, worker_id="w1", stop_event=stop_event,
            batch_size=5, lease_seconds=30, handle_item=handle_item,
            poll_interval=5.0,
        ),
        timeout=1.0,
    )

    assert called is False


class TestHeartbeatWhile:
    @pytest.mark.asyncio
    async def test_extends_the_lease_while_the_body_runs(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        queue = WorkQueue(migrated_db, throwaway_queue_table)
        claimed = await queue.claim_batch(worker_id="w1", batch_size=1, lease_seconds=1)
        first_lease = claimed[0]["lease_expires_at"]

        # Several heartbeat intervals' worth of margin against scheduling
        # jitter under load -- the assertion only needs one beat to have
        # actually landed, not a precise count of them.
        async with heartbeat_while(
            queue, row_id, worker_id="w1", lease_seconds=10, interval_seconds=0.1,
        ):
            await asyncio.sleep(0.6)

        row = await _row(migrated_db, throwaway_queue_table, row_id)
        assert row.lease_expires_at > first_lease

    @pytest.mark.asyncio
    async def test_stops_heartbeating_once_the_body_exits(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """The heartbeat task is cancelled on exit -- it must not go on
        firing (and racing a later claim's own guard predicates) after the
        row it was keeping alive has already been released."""
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        queue = WorkQueue(migrated_db, throwaway_queue_table)
        await queue.claim_batch(worker_id="w1", batch_size=1, lease_seconds=30)

        async with heartbeat_while(
            queue, row_id, worker_id="w1", lease_seconds=30, interval_seconds=0.05,
        ):
            await asyncio.sleep(0.1)

        assert await queue.complete(row_id, worker_id="w1", status="done") is True

        # Long enough that a leaked heartbeat task would have fired again;
        # it would find status != 'claimed' and simply extend nothing, but
        # a task still running here at all would mean the context manager
        # failed to cancel it.
        await asyncio.sleep(0.15)
        row = await _row(migrated_db, throwaway_queue_table, row_id)
        assert row.status == "done"


class TestDefaultWorkerLoopHeartbeat:
    @pytest.mark.asyncio
    async def test_a_slow_handler_is_not_reclaimed_out_from_under_the_worker(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """The exact failure the review describes: a call slower than its
        lease must not have its row reclaimed and re-run while the first
        call is still in flight. Without the heartbeat this wires in,
        reclaim_expired() below would return nonzero partway through and
        the handler would run twice."""
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        queue = WorkQueue(migrated_db, throwaway_queue_table)
        stop_event = asyncio.Event()
        handled: list[uuid.UUID] = []

        async def _slow_handler(row: dict) -> None:
            handled.append(row["id"])
            await asyncio.sleep(2.0)
            await queue.complete(row["id"], worker_id="w1", status="done")

        # A 1.5s lease against a real database round trip leaves the
        # heartbeat (fired every lease_seconds/3 = 0.5s) generous margin,
        # so this stays robust under load rather than racing millisecond
        # jitter the way a much tighter lease would.
        loop_task = asyncio.create_task(
            default_worker_loop(
                queue, worker_id="w1", stop_event=stop_event, batch_size=1,
                lease_seconds=1.5, handle_item=_slow_handler, poll_interval=0.05,
            )
        )

        await asyncio.sleep(0.1)  # let the claim happen
        # Reclaim repeatedly while the handler is still running past its
        # nominal 1.5s lease -- the heartbeat must keep extending it ahead
        # of every one of these.
        for _ in range(6):
            await asyncio.sleep(0.3)
            reclaimed = await queue.reclaim_expired()
            assert reclaimed == 0

        stop_event.set()
        await asyncio.wait_for(loop_task, timeout=5.0)

        assert handled == [row_id]  # exactly once -- never duplicated by a reclaim
        row = await _row(migrated_db, throwaway_queue_table, row_id)
        assert row.status == "done"
