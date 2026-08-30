"""
default_worker_loop: the canonical claim/process/release-on-stop loop,
proving a clean shutdown releases whatever in a claimed batch was never
handed to the handler.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import Table, text

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.queue.work_queue import WorkQueue
from mail_verdict.queue.worker_loop import default_worker_loop


async def _seed_rows(db: DatabaseConnection, table: Table, count: int) -> list[uuid.UUID]:
    ids = [uuid.uuid4() for _ in range(count)]
    async with db.session() as session:
        for row_id in ids:
            await session.execute(
                text(f"INSERT INTO {table.name} (id, priority) VALUES (:id, 100)"),
                {"id": row_id},
            )
    return ids


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
