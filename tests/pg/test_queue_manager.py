"""
QueueManager: persisted operator state, concurrency validated against the
database pool, and an end-to-end pause/resume against a real supervisor
claiming from a throwaway table.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest
from sqlalchemy import Table, text

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.queue.circuit import CircuitBreaker, CircuitState
from mail_verdict.queue.manager import QueueManager
from mail_verdict.queue.supervisor import WorkerBody
from mail_verdict.queue.work_queue import WorkQueue

_POLL_TIMEOUT_S = 5.0


async def _wait_until(
    predicate: Callable[[], Awaitable[bool]], *, timeout: float = _POLL_TIMEOUT_S,
) -> None:
    """Poll an async zero-arg predicate until it returns truthy or time out."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")


def _draining_worker_body(work_queue: WorkQueue) -> WorkerBody:
    """A worker body that claims and immediately completes everything pending --
    enough to prove a supervisor's concurrency setting actually gates work."""

    async def body(worker_id: str, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            claimed = await work_queue.claim_batch(
                worker_id=worker_id, batch_size=5, lease_seconds=30,
            )
            if not claimed:
                await asyncio.sleep(0.02)
                continue
            for row in claimed:
                await work_queue.complete(row["id"], worker_id=worker_id, status="done")

    return body


async def _seed_rows(db: DatabaseConnection, table: Table, count: int) -> list[uuid.UUID]:
    ids = [uuid.uuid4() for _ in range(count)]
    async with db.session() as session:
        for row_id in ids:
            await session.execute(
                text(f"INSERT INTO {table.name} (id, priority) VALUES (:id, 100)"),
                {"id": row_id},
            )
    return ids


class TestConcurrencyValidation:
    @pytest.mark.asyncio
    async def test_rejects_concurrency_above_the_pool_capacity(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """migrated_db is pool_size=5, max_overflow=0 -- a setting that cannot
        actually run that wide must fail here, not at 3am."""
        manager = QueueManager(migrated_db)
        manager.register(
            "test-queue", throwaway_queue_table,
            _draining_worker_body(WorkQueue(migrated_db, throwaway_queue_table)),
        )

        with pytest.raises(ValueError, match="exceeding"):
            await manager.set_state("test-queue", concurrency=migrated_db.pool_capacity + 1)

    @pytest.mark.asyncio
    async def test_accepts_concurrency_at_the_pool_capacity(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        manager = QueueManager(migrated_db)
        manager.register(
            "test-queue", throwaway_queue_table,
            _draining_worker_body(WorkQueue(migrated_db, throwaway_queue_table)),
        )

        summary = await manager.set_state("test-queue", concurrency=migrated_db.pool_capacity)

        assert summary.concurrency_target == migrated_db.pool_capacity

    @pytest.mark.asyncio
    async def test_rejects_a_reservation_that_leaves_no_room(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """reserved_for_requests shrinks the budget queues may fight over --
        a queue alone still cannot claim connections held back for the API."""
        manager = QueueManager(migrated_db, reserved_for_requests=2)
        manager.register(
            "test-queue", throwaway_queue_table,
            _draining_worker_body(WorkQueue(migrated_db, throwaway_queue_table)),
        )
        budget = migrated_db.pool_capacity - 2

        with pytest.raises(ValueError, match="exceeding"):
            await manager.set_state("test-queue", concurrency=budget + 1)

        summary = await manager.set_state("test-queue", concurrency=budget)
        assert summary.concurrency_target == budget

    @pytest.mark.asyncio
    async def test_rejects_when_another_running_queue_already_claims_the_budget(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """Two queues, each individually within the pool's capacity, can
        still jointly ask for more connections than the pool has -- the
        combined total across every registered queue is what is checked,
        not either queue's own request in isolation."""
        manager = QueueManager(migrated_db)
        body = _draining_worker_body(WorkQueue(migrated_db, throwaway_queue_table))
        manager.register("queue-a", throwaway_queue_table, body)
        manager.register("queue-b", throwaway_queue_table, body)
        await manager.set_state("queue-a", state="running", concurrency=3)

        with pytest.raises(ValueError, match="exceeding"):
            await manager.set_state("queue-b", state="running", concurrency=3)

        summary = await manager.set_state("queue-b", state="running", concurrency=2)
        assert summary.concurrency_target == 2

    @pytest.mark.asyncio
    async def test_resuming_a_paused_queue_is_checked_even_without_a_concurrency_change(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """A bare state="running" with no concurrency argument still lets
        the queue's already-persisted concurrency take effect -- that must
        be checked against the budget too, not only a call that also
        changes concurrency in the same request."""
        manager = QueueManager(migrated_db)
        body = _draining_worker_body(WorkQueue(migrated_db, throwaway_queue_table))
        manager.register("queue-a", throwaway_queue_table, body)
        manager.register("queue-b", throwaway_queue_table, body)
        await manager.set_state("queue-a", state="paused")
        await manager.set_state("queue-b", state="running", concurrency=5)
        # Allowed while paused: a queue not currently running does not
        # compete for the pool, whatever its stored concurrency says.
        await manager.set_state("queue-a", concurrency=3)

        with pytest.raises(ValueError, match="exceeding"):
            await manager.set_state("queue-a", state="running")

    @pytest.mark.asyncio
    async def test_start_clamps_a_persisted_state_that_predates_the_check(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """A queue_state row written before this validation existed (or
        edited directly) is read exactly as stored -- start() must not
        apply it uncapped, or a restart silently recreates the pool
        starvation the check exists to prevent."""
        manager = QueueManager(migrated_db)
        body = _draining_worker_body(WorkQueue(migrated_db, throwaway_queue_table))
        manager.register("queue-a", throwaway_queue_table, body)
        manager.register("queue-b", throwaway_queue_table, body)
        async with migrated_db.session() as session:
            for name in ("queue-a", "queue-b"):
                await session.execute(
                    text(
                        "INSERT INTO queue_state (name, state, concurrency) "
                        "VALUES (:name, 'running', :concurrency) "
                        "ON CONFLICT (name) DO UPDATE SET "
                        "state = EXCLUDED.state, concurrency = EXCLUDED.concurrency"
                    ),
                    {"name": name, "concurrency": migrated_db.pool_capacity},
                )

        try:
            await manager.start()
            summary_a = await manager.summary("queue-a")
            summary_b = await manager.summary("queue-b")
            assert summary_a.concurrency_target + summary_b.concurrency_target <= (
                migrated_db.pool_capacity
            )
        finally:
            await manager.stop(drain_timeout=2.0)


class TestUnregisteredQueue:
    @pytest.mark.asyncio
    async def test_summary_of_an_unknown_name_raises(self, migrated_db: DatabaseConnection) -> None:
        manager = QueueManager(migrated_db)
        with pytest.raises(KeyError):
            await manager.summary("nope")

    @pytest.mark.asyncio
    async def test_set_state_of_an_unknown_name_raises(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        manager = QueueManager(migrated_db)
        with pytest.raises(KeyError):
            await manager.set_state("nope", state="paused")


class TestResetCircuit:
    @pytest.mark.asyncio
    async def test_forces_a_suspended_breaker_closed(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """The operator's manual recovery path: right after a missing or
        rejected credential is fixed, this closes the breaker immediately
        rather than waiting out its own probe interval."""
        name = f"queue-{uuid.uuid4().hex[:8]}"
        circuit_name = f"provider-{uuid.uuid4().hex[:8]}"
        manager = QueueManager(migrated_db)
        manager.register(
            name, throwaway_queue_table,
            _draining_worker_body(WorkQueue(migrated_db, throwaway_queue_table)),
            circuit_name=circuit_name,
        )
        breaker = CircuitBreaker(migrated_db, circuit_name)
        await breaker.record_unavailable(
            reason="no key configured", probe_interval=timedelta(minutes=5),
        )
        assert (await breaker.status()).state == CircuitState.SUSPENDED

        summary = await manager.reset_circuit(name)

        assert summary.circuit.state == CircuitState.CLOSED
        assert (await breaker.status()).state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_resets_the_breaker_the_queue_is_actually_registered_under(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """Resetting queue A must never touch queue B's breaker, even when
        both happen to be suspended -- the reset is scoped by the queue's
        own registered circuit_name, not by name alone."""
        name_a = f"queue-{uuid.uuid4().hex[:8]}"
        name_b = f"queue-{uuid.uuid4().hex[:8]}"
        circuit_a = f"provider-{uuid.uuid4().hex[:8]}"
        circuit_b = f"provider-{uuid.uuid4().hex[:8]}"
        manager = QueueManager(migrated_db)
        body = _draining_worker_body(WorkQueue(migrated_db, throwaway_queue_table))
        manager.register(name_a, throwaway_queue_table, body, circuit_name=circuit_a)
        manager.register(name_b, throwaway_queue_table, body, circuit_name=circuit_b)
        for circuit_name in (circuit_a, circuit_b):
            await CircuitBreaker(migrated_db, circuit_name).record_unavailable(
                reason="no key configured", probe_interval=timedelta(minutes=5),
            )

        await manager.reset_circuit(name_a)

        assert (await CircuitBreaker(migrated_db, circuit_a).status()).state == CircuitState.CLOSED
        assert (
            await CircuitBreaker(migrated_db, circuit_b).status()
        ).state == CircuitState.SUSPENDED

    @pytest.mark.asyncio
    async def test_a_resolved_circuit_name_follows_the_live_setting(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """A queue whose provider is a runtime setting registers a resolver,
        not a fixed name -- so switching the provider moves the reported
        breaker with it instead of leaving the readout pointing at one
        nothing writes to."""
        name = f"queue-{uuid.uuid4().hex[:8]}"
        circuit_a = f"provider-{uuid.uuid4().hex[:8]}"
        circuit_b = f"provider-{uuid.uuid4().hex[:8]}"
        selected = circuit_a
        manager = QueueManager(migrated_db)
        manager.register(
            name, throwaway_queue_table,
            _draining_worker_body(WorkQueue(migrated_db, throwaway_queue_table)),
            circuit_name=lambda: selected,
        )
        await CircuitBreaker(migrated_db, circuit_b).record_unavailable(
            reason="no key configured", probe_interval=timedelta(minutes=5),
        )

        assert (await manager.summary(name)).circuit.state == CircuitState.CLOSED
        selected = circuit_b
        assert (await manager.summary(name)).circuit.state == CircuitState.SUSPENDED

    @pytest.mark.asyncio
    async def test_of_an_unknown_name_raises(self, migrated_db: DatabaseConnection) -> None:
        manager = QueueManager(migrated_db)
        with pytest.raises(KeyError):
            await manager.reset_circuit("nope")


class TestConcurrentStateWrites:
    @pytest.mark.asyncio
    async def test_two_simultaneous_raises_cannot_jointly_exceed_the_budget(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """The sequential version of this passes even with the check and the
        write unsynchronised, because the first call has already written by
        the time the second reads. Two calls issued together do not reliably
        interleave either -- the event loop is free to run one to completion.

        So the interleaving is forced: both calls are held after reading the
        committed budget and released together, which is the state two
        `PATCH /api/queues/{name}` requests landing close together reach on
        their own. Each then sees a budget the other is about to spend, and
        both pass a check whose entire purpose is that they should not.
        """
        manager = QueueManager(migrated_db, reserved_for_requests=0)
        body = _draining_worker_body(WorkQueue(migrated_db, throwaway_queue_table))
        name_a = f"queue-{uuid.uuid4().hex[:8]}"
        name_b = f"queue-{uuid.uuid4().hex[:8]}"
        manager.register(name_a, throwaway_queue_table, body)
        manager.register(name_b, throwaway_queue_table, body)

        budget = migrated_db.pool_capacity
        each = budget // 2 + 1  # any two of these together overrun the pool

        both_have_read = asyncio.Barrier(2)
        real_committed = manager._committed_concurrency

        async def _read_then_wait(**kwargs: object) -> int:
            total = await real_committed(**kwargs)  # type: ignore[arg-type]
            # Serialized correctly, the second call never reaches this point
            # while the first holds the lock, so the wait simply expires and
            # the first proceeds. Unsynchronised, both arrive at once and are
            # released together, each holding a budget read taken before the
            # other's write.
            with contextlib.suppress(TimeoutError, asyncio.BrokenBarrierError):
                await asyncio.wait_for(both_have_read.wait(), timeout=2.0)
            return total

        manager._committed_concurrency = _read_then_wait  # type: ignore[method-assign]

        results = await asyncio.gather(
            manager.set_state(name_a, state="running", concurrency=each),
            manager.set_state(name_b, state="running", concurrency=each),
            return_exceptions=True,
        )

        accepted = [r for r in results if not isinstance(r, BaseException)]
        assert len(accepted) == 1, (
            f"both raises were accepted, committing {each * 2} against a budget of {budget}"
        )
        assert any(isinstance(r, ValueError) for r in results)

        committed = 0
        for queue_name in (name_a, name_b):
            row = await manager.get_state(queue_name)
            if row.state == "running":
                committed += row.concurrency
        assert committed <= budget


class TestPersistedState:
    @pytest.mark.asyncio
    async def test_state_survives_a_fresh_manager_instance(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """A second QueueManager against the same database -- standing in for a
        process restart -- reads back what the first one wrote, rather than
        resetting to a hardcoded default."""
        name = f"queue-{uuid.uuid4().hex[:8]}"
        work_queue = WorkQueue(migrated_db, throwaway_queue_table)

        manager_one = QueueManager(migrated_db)
        manager_one.register(name, throwaway_queue_table, _draining_worker_body(work_queue))
        await manager_one.set_state(name, state="paused", concurrency=3)

        manager_two = QueueManager(migrated_db)
        manager_two.register(name, throwaway_queue_table, _draining_worker_body(work_queue))
        row = await manager_two.get_state(name)

        assert row.state == "paused"
        assert row.concurrency == 3


class TestPauseAndResume:
    @pytest.mark.asyncio
    async def test_paused_queue_claims_nothing_until_resumed(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        name = f"queue-{uuid.uuid4().hex[:8]}"
        work_queue = WorkQueue(migrated_db, throwaway_queue_table)
        manager = QueueManager(migrated_db)
        manager.register(name, throwaway_queue_table, _draining_worker_body(work_queue))
        await manager.set_state(name, state="paused", concurrency=2)
        await manager.start()

        try:
            await _seed_rows(migrated_db, throwaway_queue_table, 5)
            await asyncio.sleep(0.3)
            counts = await work_queue.counts_by_status()
            assert counts.get("done", 0) == 0
            assert counts.get("pending", 0) == 5

            await manager.set_state(name, state="running")

            async def _all_done() -> bool:
                counts = await work_queue.counts_by_status()
                return counts.get("done", 0) == 5

            await _wait_until(_all_done)
        finally:
            await manager.stop(drain_timeout=2.0)

    @pytest.mark.asyncio
    async def test_running_queue_drains_seeded_rows(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        name = f"queue-{uuid.uuid4().hex[:8]}"
        work_queue = WorkQueue(migrated_db, throwaway_queue_table)
        manager = QueueManager(migrated_db)
        manager.register(name, throwaway_queue_table, _draining_worker_body(work_queue))
        await manager.set_state(name, state="running", concurrency=2)
        await manager.start()

        try:
            await _seed_rows(migrated_db, throwaway_queue_table, 10)

            async def _all_done() -> bool:
                counts = await work_queue.counts_by_status()
                return counts.get("done", 0) == 10

            await _wait_until(_all_done)
        finally:
            await manager.stop(drain_timeout=2.0)


class TestAutomaticLeaseReclaim:
    @pytest.mark.asyncio
    async def test_start_reclaims_expired_leases_without_a_dedicated_worker(
        self, migrated_db: DatabaseConnection, throwaway_queue_table: Table,
    ) -> None:
        """A row a dead worker never got back to still recovers on its own once
        the manager is running -- the reconciliation timer this wires up, not
        the queue's own workers, is what makes that automatic."""
        name = f"queue-{uuid.uuid4().hex[:8]}"
        work_queue = WorkQueue(migrated_db, throwaway_queue_table)
        [row_id] = await _seed_rows(migrated_db, throwaway_queue_table, 1)
        # A worker claims it and dies -- simulated by backdating the lease
        # rather than actually racing a real timeout.
        await work_queue.claim_batch(worker_id="dead-worker", batch_size=1, lease_seconds=30)
        async with migrated_db.session() as session:
            await session.execute(
                text(
                    f"UPDATE {throwaway_queue_table.name} "
                    "SET lease_expires_at = now() - interval '1 second' WHERE id = :id"
                ),
                {"id": row_id},
            )

        manager = QueueManager(migrated_db, reclaim_interval_seconds=0.1)
        manager.register(name, throwaway_queue_table, _draining_worker_body(work_queue))
        # Paused so this queue's own supervisor never claims it -- isolating
        # the assertion to the reclaim timer having run.
        await manager.set_state(name, state="paused")
        await manager.start()

        try:
            async def _reclaimed() -> bool:
                counts = await work_queue.counts_by_status()
                return counts.get("pending", 0) == 1

            await _wait_until(_reclaimed, timeout=3.0)
        finally:
            await manager.stop(drain_timeout=2.0)
