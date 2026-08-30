"""
The registry tying a named queue's WorkQueue, WorkerSupervisor and
CircuitBreaker together, and the persisted operator state (pause/resume,
concurrency, batch size) that survives a restart.

Still domain-agnostic: registering a queue takes a table and a worker body
coroutine, nothing that knows what the table holds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text

from mail_verdict.queue.circuit import CircuitBreaker, CircuitStatus
from mail_verdict.queue.notify import ReconciliationTimer
from mail_verdict.queue.supervisor import WorkerSupervisor
from mail_verdict.queue.work_queue import WorkQueue

if TYPE_CHECKING:
    from sqlalchemy import Table

    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.queue.supervisor import WorkerBody

logger = logging.getLogger(__name__)

# Arbitrary, stable advisory lock key for this manager's lease-reclaim
# timer -- must not collide with a key any other reconciliation timer in
# the process picks (a sweep controller, say). Fits a bigint comfortably.
_RECLAIM_LOCK_KEY = 761_034_221


@dataclass(frozen=True)
class QueueStateRow:
    """The persisted operator-controlled row for one named queue."""

    name: str
    state: str
    concurrency: int
    batch_size: int


@dataclass(frozen=True)
class QueueSummary:
    """Everything an observability surface needs about one queue."""

    name: str
    state: str
    concurrency_target: int
    concurrency_actual: int
    max_allowed_concurrency: int
    batch_size: int
    depth: dict[str, int]
    circuit: CircuitStatus


class _RegisteredQueue:
    """Internal bookkeeping for one queue registered with the manager."""

    def __init__(
        self, work_queue: WorkQueue, supervisor: WorkerSupervisor, circuit_name: str,
    ) -> None:
        self.work_queue = work_queue
        self.supervisor = supervisor
        self.circuit_name = circuit_name


class QueueManager:
    """Registry of named queues, and the REST-facing operations on them."""

    def __init__(self, db: DatabaseConnection, *, reclaim_interval_seconds: float = 5.0) -> None:
        """
        Args:
            db: Database connection every queue's state lives behind
            reclaim_interval_seconds: How often the shared, advisory-locked
                reconciliation timer reclaims expired leases across every
                registered queue
        """
        self._db = db
        self._registered: dict[str, _RegisteredQueue] = {}
        self._reclaim_timer = ReconciliationTimer(
            db, _RECLAIM_LOCK_KEY, self._reclaim_all, reclaim_interval_seconds,
        )

    async def _reclaim_all(self) -> None:
        """Reclaim expired leases on every registered queue -- the
        reconciliation timer's callback, run behind its advisory lock."""
        for entry in self._registered.values():
            await entry.work_queue.reclaim_expired()

    def register(
        self,
        name: str,
        table: Table,
        worker_body: WorkerBody,
        *,
        circuit_name: str | None = None,
        reconcile_interval_seconds: float = 1.0,
    ) -> WorkQueue:
        """
        Register a named queue against a table.

        Args:
            name: Unique queue name, used in the API and the NOTIFY channel
            table: Table backing this queue's work rows
            worker_body: Coroutine run per worker task
            circuit_name: Circuit breaker name to report for this queue;
                defaults to the queue's own name, so two queues that should
                share a provider's breaker must pass the same name
                explicitly
            reconcile_interval_seconds: Supervisor's periodic safety-net tick

        Returns:
            The WorkQueue created for this registration
        """
        work_queue = WorkQueue(self._db, table)
        supervisor = WorkerSupervisor(
            name, worker_body, reconcile_interval_seconds=reconcile_interval_seconds,
        )
        self._registered[name] = _RegisteredQueue(work_queue, supervisor, circuit_name or name)
        return work_queue

    def names(self) -> list[str]:
        """Names of every registered queue."""
        return list(self._registered)

    def work_queue(self, name: str) -> WorkQueue:
        """The WorkQueue for a registered name, for the worker body to claim from."""
        return self._registered[name].work_queue

    async def start(self) -> None:
        """Load persisted state for every registered queue, start its supervisor,
        and start the shared lease-reclaim timer."""
        for name, entry in self._registered.items():
            row = await self._get_or_create_state(name)
            entry.supervisor.set_target(row.concurrency if row.state == "running" else 0)
            await entry.supervisor.start()
        await self._reclaim_timer.start()

    async def stop(self, *, drain_timeout: float | None = None) -> None:
        """Stop the reclaim timer and every registered queue's supervisor,
        draining in-flight work."""
        await self._reclaim_timer.stop()
        for entry in self._registered.values():
            await entry.supervisor.stop(drain_timeout=drain_timeout)

    async def get_state(self, name: str) -> QueueStateRow:
        """The persisted operator state for a registered queue."""
        if name not in self._registered:
            raise KeyError(name)
        return await self._get_or_create_state(name)

    async def summary(self, name: str) -> QueueSummary:
        """
        A full snapshot of one queue for the observability surface.

        Args:
            name: Registered queue name

        Returns:
            Current state, concurrency, depth by status, and circuit health
        """
        if name not in self._registered:
            raise KeyError(name)
        entry = self._registered[name]
        row = await self._get_or_create_state(name)
        depth = await entry.work_queue.counts_by_status()
        circuit = await CircuitBreaker(self._db, entry.circuit_name).status()
        return QueueSummary(
            name=name,
            state=row.state,
            concurrency_target=entry.supervisor.target,
            concurrency_actual=entry.supervisor.actual,
            max_allowed_concurrency=self._db.pool_capacity,
            batch_size=row.batch_size,
            depth=depth,
            circuit=circuit,
        )

    async def list_summaries(self) -> list[QueueSummary]:
        """A summary for every registered queue."""
        return [await self.summary(name) for name in self._registered]

    async def set_state(
        self,
        name: str,
        *,
        state: str | None = None,
        concurrency: int | None = None,
        batch_size: int | None = None,
    ) -> QueueSummary:
        """
        Change a queue's operator-controlled state, applied to the live
        supervisor immediately and persisted so it survives a restart.

        Args:
            name: Registered queue name
            state: 'running' or 'paused', or None to leave unchanged
            concurrency: New target worker count, or None to leave unchanged
            batch_size: New claim batch size, or None to leave unchanged

        Returns:
            The resulting summary

        Raises:
            KeyError: name is not registered
            ValueError: state is not 'running'/'paused', or concurrency
                exceeds what the database pool can actually support
        """
        if name not in self._registered:
            raise KeyError(name)
        if state is not None and state not in ("running", "paused"):
            raise ValueError(f"state must be 'running' or 'paused', got {state!r}")
        if concurrency is not None:
            if concurrency < 0:
                raise ValueError("concurrency must be >= 0")
            if concurrency > self._db.pool_capacity:
                raise ValueError(
                    f"concurrency {concurrency} exceeds the database pool capacity "
                    f"({self._db.pool_capacity} = pool_size + max_overflow)"
                )
        if batch_size is not None and batch_size < 1:
            raise ValueError("batch_size must be >= 1")

        await self._get_or_create_state(name)
        async with self._db.session() as session:
            sets = []
            params: dict[str, object] = {"name": name}
            if state is not None:
                sets.append("state = :state")
                params["state"] = state
            if concurrency is not None:
                sets.append("concurrency = :concurrency")
                params["concurrency"] = concurrency
            if batch_size is not None:
                sets.append("batch_size = :batch_size")
                params["batch_size"] = batch_size
            if sets:
                await session.execute(
                    text(f"UPDATE queue_state SET {', '.join(sets)} WHERE name = :name"),
                    params,
                )

        row = await self._get_or_create_state(name)
        entry = self._registered[name]
        entry.supervisor.set_target(row.concurrency if row.state == "running" else 0)
        return await self.summary(name)

    async def _get_or_create_state(self, name: str) -> QueueStateRow:
        async with self._db.session() as session:
            await session.execute(
                text(
                    "INSERT INTO queue_state (name) VALUES (:name) "
                    "ON CONFLICT (name) DO NOTHING"
                ),
                {"name": name},
            )
            result = await session.execute(
                text(
                    "SELECT name, state, concurrency, batch_size FROM queue_state "
                    "WHERE name = :name"
                ),
                {"name": name},
            )
            row = result.one()
            return QueueStateRow(
                name=row.name, state=row.state,
                concurrency=row.concurrency, batch_size=row.batch_size,
            )


_manager: QueueManager | None = None


def init_queue_manager(db: DatabaseConnection) -> QueueManager:
    """Initialize the global QueueManager."""
    global _manager
    _manager = QueueManager(db)
    return _manager


def get_queue_manager() -> QueueManager:
    """
    Get the global QueueManager.

    Raises:
        RuntimeError: If not initialized
    """
    if _manager is None:
        raise RuntimeError("QueueManager not initialized")
    return _manager


def reset_queue_manager() -> None:
    """Clear the global QueueManager -- test teardown."""
    global _manager
    _manager = None
