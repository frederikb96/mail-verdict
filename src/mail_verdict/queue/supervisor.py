"""
Runtime-changeable worker concurrency.

An asyncio task per worker, inside the same process as the API -- not a
worker process, since the workers here are just asyncio tasks and the
supervisor that manages them is a couple dozen lines. See the pipeline
design's case against a queue library for why that trade holds for a
single-replica deployment.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

WorkerBody = Callable[[str, "asyncio.Event"], Awaitable[None]]

_DEFAULT_RECONCILE_INTERVAL_S = 1.0


class WorkerSupervisor:
    """
    Maintains N asyncio worker tasks against a live, changeable target.

    Retiring a worker asks it to stop rather than cancelling it -- the
    worker body is expected to check its stop event between items (or
    between claim batches) and return cleanly, so a concurrency decrease
    never interrupts an item in flight.
    """

    def __init__(
        self,
        name: str,
        worker_body: WorkerBody,
        *,
        initial_concurrency: int = 0,
        reconcile_interval_seconds: float = _DEFAULT_RECONCILE_INTERVAL_S,
    ) -> None:
        """
        Args:
            name: Used as a prefix for worker ids and task names
            worker_body: Coroutine run per worker; receives its own worker
                id and a stop event it should check between units of work
            initial_concurrency: Target worker count to start at
            reconcile_interval_seconds: Periodic safety-net tick, in
                addition to the immediate reconcile `set_target` triggers
        """
        self._name = name
        self._worker_body = worker_body
        self._target = max(0, initial_concurrency)
        self._reconcile_interval = reconcile_interval_seconds
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()
        self._reconcile_task: asyncio.Task[None] | None = None

    @property
    def target(self) -> int:
        """The concurrency this supervisor is currently reconciling toward."""
        return self._target

    @property
    def actual(self) -> int:
        """Live worker task count, including ones mid-retirement.

        Counts not-done tasks directly rather than reading len(self._tasks)
        -- the dict is only pruned by the reconcile tick, so between a
        worker exiting and the next tick (including after stop(), which
        does not tick again) the dict itself would overcount.
        """
        return sum(1 for task in self._tasks.values() if not task.done())

    async def start(self) -> None:
        """Start the periodic reconciliation loop and reach the initial target."""
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(), name=f"{self._name}-supervisor",
        )
        await self._reconcile_once()

    def set_target(self, concurrency: int) -> None:
        """
        Change the target worker count, taking effect almost immediately.

        Args:
            concurrency: New target; negative values clamp to 0
        """
        self._target = max(0, concurrency)
        if self._reconcile_task is not None:
            asyncio.ensure_future(self._reconcile_once())

    async def stop(self, *, drain_timeout: float | None = None) -> None:
        """
        Stop reconciling and ask every live worker to retire, waiting up to
        `drain_timeout` for them to finish their current item.

        Args:
            drain_timeout: Seconds to wait for workers to exit; None waits
                indefinitely
        """
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconcile_task
            self._reconcile_task = None

        for event in self._stop_events.values():
            event.set()
        if self._tasks:
            await asyncio.wait(self._tasks.values(), timeout=drain_timeout)

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self._reconcile_interval)
            await self._reconcile_once()

    async def _reconcile_once(self) -> None:
        async with self._lock:
            for worker_id in [wid for wid, t in self._tasks.items() if t.done()]:
                del self._tasks[worker_id]
                del self._stop_events[worker_id]

            deficit = self._target - len(self._tasks)
            if deficit > 0:
                for _ in range(deficit):
                    self._spawn_one()
            elif deficit < 0:
                retiring = list(self._tasks)[: -deficit]
                for worker_id in retiring:
                    self._stop_events[worker_id].set()

    def _spawn_one(self) -> None:
        worker_id = f"{self._name}-{uuid.uuid4().hex[:8]}"
        stop_event = asyncio.Event()
        task = asyncio.create_task(self._run_worker(worker_id, stop_event), name=worker_id)
        self._tasks[worker_id] = task
        self._stop_events[worker_id] = stop_event

    async def _run_worker(self, worker_id: str, stop_event: asyncio.Event) -> None:
        try:
            await self._worker_body(worker_id, stop_event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker body raised", extra={"worker_id": worker_id})
