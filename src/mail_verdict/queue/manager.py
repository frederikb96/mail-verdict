"""
The registry tying a named queue's WorkQueue, WorkerSupervisor and
CircuitBreaker together, and the persisted operator state (pause/resume,
concurrency) that survives a restart.

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
    from collections.abc import Callable

    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncSession

    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.queue.supervisor import WorkerBody

logger = logging.getLogger(__name__)

# Arbitrary, stable advisory lock key for this manager's lease-reclaim
# timer -- must not collide with a key any other reconciliation timer in
# the process picks (a sweep controller, say). Fits a bigint comfortably.
_RECLAIM_LOCK_KEY = 761_034_221

# Serializes the read-then-write in set_state across processes. Reading every
# queue's committed concurrency and writing this one's are two round trips, so
# two callers that both read before either writes each see a budget the other
# is about to spend -- and both pass a check whose whole purpose is that they
# should not. Distinct from the reclaim key above; they guard different things
# and must never wait on each other.
_STATE_WRITE_LOCK_KEY = 761_034_222


@dataclass(frozen=True)
class QueueStateRow:
    """The persisted operator-controlled row for one named queue."""

    name: str
    state: str
    concurrency: int


@dataclass(frozen=True)
class QueueSummary:
    """Everything an observability surface needs about one queue."""

    name: str
    state: str
    concurrency_target: int
    concurrency_actual: int
    max_allowed_concurrency: int
    depth: dict[str, int]
    circuit: CircuitStatus


class _RegisteredQueue:
    """Internal bookkeeping for one queue registered with the manager."""

    def __init__(
        self,
        work_queue: WorkQueue,
        supervisor: WorkerSupervisor,
        circuit_name: str | Callable[[], str],
    ) -> None:
        self.work_queue = work_queue
        self.supervisor = supervisor
        self._circuit_name = circuit_name

    @property
    def circuit_name(self) -> str:
        """The breaker this queue's work actually trips, resolved now.

        A queue whose provider is a live setting resolves its name per
        call, so the reported breaker follows the setting instead of
        naming one nothing ever writes to.
        """
        if callable(self._circuit_name):
            return self._circuit_name()
        return self._circuit_name


class QueueManager:
    """Registry of named queues, and the REST-facing operations on them."""

    def __init__(
        self,
        db: DatabaseConnection,
        *,
        reserved_for_requests: int = 0,
        reclaim_interval_seconds: float = 5.0,
    ) -> None:
        """
        Args:
            db: Database connection every queue's state lives behind
            reserved_for_requests: Connections held back from every
                registered queue's combined running concurrency, so an
                HTTP handler sharing the same pool always finds one free
                (see `database.reserved_for_requests` in config.yaml,
                which is what the running application actually passes
                here -- the default of 0 here is for callers, such as
                tests, that only care about the pool's own capacity)
            reclaim_interval_seconds: How often the shared, advisory-locked
                reconciliation timer reclaims expired leases across every
                registered queue
        """
        self._db = db
        self._reserved_for_requests = max(0, reserved_for_requests)
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
        circuit_name: str | Callable[[], str] | None = None,
        reconcile_interval_seconds: float = 1.0,
    ) -> WorkQueue:
        """
        Register a named queue against a table.

        Args:
            name: Unique queue name, used in the API and the NOTIFY channel
            table: Table backing this queue's work rows
            worker_body: Coroutine run per worker task
            circuit_name: Circuit breaker name to report for this queue,
                or a callable resolving it when the queue's provider is a
                live setting. Defaults to the queue's own name, so two
                queues that should share a provider's breaker must pass
                the same name explicitly
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
        """
        Load persisted state for every registered queue, start its
        supervisor, and start the shared lease-reclaim timer.

        Clamps each queue's target to what remains of the budget after
        every queue started earlier in this call -- `set_state` is what
        normally keeps a persisted value inside that budget, but a row
        written before this validation existed, or edited directly, is
        read exactly as stored. Applying it uncapped here would recreate
        the pool starvation the validation exists to prevent, silently,
        on every restart.
        """
        budget = self._db.pool_capacity - self._reserved_for_requests
        for name, entry in self._registered.items():
            row = await self._get_or_create_state(name)
            target = row.concurrency if row.state == "running" else 0
            if target > budget:
                logger.warning(
                    "Queue's persisted concurrency exceeds the remaining pool budget; clamping",
                    extra={"queue": name, "persisted": target, "available": max(budget, 0)},
                )
                target = max(budget, 0)
            entry.supervisor.set_target(target)
            budget -= target
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
        committed = await self._committed_concurrency(exclude=name)
        max_allowed = max(self._db.pool_capacity - self._reserved_for_requests - committed, 0)
        return QueueSummary(
            name=name,
            state=row.state,
            concurrency_target=entry.supervisor.target,
            concurrency_actual=entry.supervisor.actual,
            max_allowed_concurrency=max_allowed,
            depth=depth,
            circuit=circuit,
        )

    async def _committed_concurrency(
        self, *, exclude: str | None = None, session: AsyncSession | None = None,
    ) -> int:
        """
        Sum of persisted concurrency across every other *running*
        registered queue -- what already stands against the shared
        connection pool, independent of whatever `exclude` is about to be
        set to.

        Args:
            exclude: Queue name to leave out of the sum -- the one being
                validated or reported on
            session: Read through this session rather than opening one, so
                a caller holding a lock sees the state that lock protects

        Returns:
            Combined concurrency every other running queue has committed
        """
        total = 0
        for other_name in self._registered:
            if other_name == exclude:
                continue
            row = await self._get_or_create_state(other_name, session=session)
            if row.state == "running":
                total += row.concurrency
        return total

    async def list_summaries(self) -> list[QueueSummary]:
        """A summary for every registered queue."""
        return [await self.summary(name) for name in self._registered]

    async def reset_circuit(self, name: str) -> QueueSummary:
        """
        Force a queue's circuit breaker closed.

        The operator's manual recovery path: a breaker suspended on a
        missing or rejected credential clears itself only through
        `try_probe`, which a worker attempts on its own schedule -- this
        is for closing it immediately, right after the credential that
        caused the suspension has just been fixed, rather than waiting out
        the probe interval.

        Args:
            name: Registered queue name

        Returns:
            The resulting summary

        Raises:
            KeyError: name is not registered
        """
        if name not in self._registered:
            raise KeyError(name)
        entry = self._registered[name]
        await CircuitBreaker(self._db, entry.circuit_name).record_success()
        return await self.summary(name)

    async def set_state(
        self,
        name: str,
        *,
        state: str | None = None,
        concurrency: int | None = None,
    ) -> QueueSummary:
        """
        Change a queue's operator-controlled state, applied to the live
        supervisor immediately and persisted so it survives a restart.

        Validated against every other registered queue's own persisted
        concurrency, not just this queue's own request in isolation --
        two queues each individually within the database pool's capacity
        can still jointly claim more connections than the pool has to
        give, starving the HTTP handlers that share it. The check re-runs
        whenever this queue's *effective* running concurrency would
        change, which includes resuming a paused queue with `state=
        "running"` alone: its already-persisted concurrency is what takes
        effect the moment it resumes, whether or not this call also
        touches `concurrency`.

        Args:
            name: Registered queue name
            state: 'running' or 'paused', or None to leave unchanged
            concurrency: New target worker count, or None to leave unchanged

        Returns:
            The resulting summary

        Raises:
            KeyError: name is not registered
            ValueError: state is not 'running'/'paused', concurrency is
                negative, or the resulting running concurrency -- combined
                with every other running queue's own -- would exceed what
                the database pool can actually support
        """
        if name not in self._registered:
            raise KeyError(name)
        if state is not None and state not in ("running", "paused"):
            raise ValueError(f"state must be 'running' or 'paused', got {state!r}")
        if concurrency is not None and concurrency < 0:
            raise ValueError("concurrency must be >= 0")

        # One transaction, holding an advisory lock, for the budget check and
        # the write it authorises. Apart they are two round trips, so two
        # callers can both read a budget the other is about to spend and both
        # pass -- jointly claiming more of the connection pool than it has,
        # which is the single thing this check exists to prevent. The lock is
        # released when the transaction ends, however it ends.
        async with self._db.session() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STATE_WRITE_LOCK_KEY},
            )
            current = await self._get_or_create_state(name, session=session)
            effective_state = state if state is not None else current.state
            effective_concurrency = (
                concurrency if concurrency is not None else current.concurrency
            )

            if effective_state == "running" and effective_concurrency > 0:
                committed = await self._committed_concurrency(exclude=name, session=session)
                budget = self._db.pool_capacity - self._reserved_for_requests
                if committed + effective_concurrency > budget:
                    raise ValueError(
                        f"concurrency {effective_concurrency} for {name!r} would bring the "
                        f"combined running concurrency across every queue to "
                        f"{committed + effective_concurrency}, exceeding the {budget} "
                        f"connections available to queues (database pool capacity "
                        f"{self._db.pool_capacity}, {self._reserved_for_requests} reserved "
                        "for non-queue requests)"
                    )

            sets = []
            params: dict[str, object] = {"name": name}
            if state is not None:
                sets.append("state = :state")
                params["state"] = state
            if concurrency is not None:
                sets.append("concurrency = :concurrency")
                params["concurrency"] = concurrency
            if sets:
                await session.execute(
                    text(f"UPDATE queue_state SET {', '.join(sets)} WHERE name = :name"),
                    params,
                )

        row = await self._get_or_create_state(name)
        entry = self._registered[name]
        entry.supervisor.set_target(row.concurrency if row.state == "running" else 0)
        return await self.summary(name)

    async def _get_or_create_state(
        self, name: str, *, session: AsyncSession | None = None,
    ) -> QueueStateRow:
        if session is not None:
            return await self._read_state(session, name)
        async with self._db.session() as owned:
            return await self._read_state(owned, name)

    @staticmethod
    async def _read_state(session: AsyncSession, name: str) -> QueueStateRow:
        await session.execute(
            text(
                "INSERT INTO queue_state (name) VALUES (:name) ON CONFLICT (name) DO NOTHING"
            ),
            {"name": name},
        )
        result = await session.execute(
            text("SELECT name, state, concurrency FROM queue_state WHERE name = :name"),
            {"name": name},
        )
        row = result.one()
        return QueueStateRow(name=row.name, state=row.state, concurrency=row.concurrency)


_manager: QueueManager | None = None


def init_queue_manager(db: DatabaseConnection, *, reserved_for_requests: int) -> QueueManager:
    """
    Initialize the global QueueManager.

    Args:
        db: Database connection every queue's state lives behind
        reserved_for_requests: Connections held back from every queue's
            combined running concurrency for the HTTP handlers sharing
            the same pool -- `config.database.reserved_for_requests`
    """
    global _manager
    _manager = QueueManager(db, reserved_for_requests=reserved_for_requests)
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
