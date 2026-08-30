"""
NOTIFY-based wakeup, and the advisory-locked reconciliation timer that
makes correctness independent of it ever firing.

A dedicated channel: never postimap_events, which belongs to PostIMAP and
carries nothing this package writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

CHANNEL = "mail_verdict_work"


class WorkQueueNotifier:
    """
    LISTENs on the shared work channel and wakes whichever in-process
    waiters are registered for the queue named in each NOTIFY payload.

    A reconnect loses any NOTIFY fired during the gap -- correctness never
    depends on this class, only latency does; ReconciliationTimer's
    periodic reclaim is the net underneath it.
    """

    def __init__(self, dsn: str) -> None:
        """
        Args:
            dsn: asyncpg-compatible DSN (postgresql://user:pass@host/db)
        """
        self._dsn = dsn
        self._conn: asyncpg.Connection | None = None
        self._events: dict[str, asyncio.Event] = {}

    def event_for(self, queue_name: str) -> asyncio.Event:
        """
        The asyncio.Event a queue's workers should wait on.

        Args:
            queue_name: Payload value a NOTIFY on this channel carries

        Returns:
            The event, created on first use and shared by every caller
        """
        return self._events.setdefault(queue_name, asyncio.Event())

    async def start(self) -> None:
        """Open the LISTEN connection."""
        self._conn = await asyncpg.connect(self._dsn)
        await self._conn.add_listener(CHANNEL, self._on_notify)

    async def stop(self) -> None:
        """Close the LISTEN connection."""
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.remove_listener(CHANNEL, self._on_notify)
            await self._conn.close()
            self._conn = None

    def _on_notify(
        self, connection: asyncpg.Connection, pid: int, channel: str, payload: str,
    ) -> None:
        self.event_for(payload).set()

    @staticmethod
    async def notify(session: AsyncSession, queue_name: str) -> None:
        """
        Wake workers for a queue -- called after enqueuing new work.

        Uses pg_notify() rather than the NOTIFY statement so the queue name
        can be a bound parameter rather than an identifier spliced into SQL.

        Args:
            session: Session to run the notification on, ideally the same
                one that just committed the enqueue
            queue_name: Payload to publish on the shared channel
        """
        await session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": CHANNEL, "payload": queue_name},
        )


async def wait_for_work(event: asyncio.Event, *, poll_interval: float) -> None:
    """
    Block until woken by NOTIFY or `poll_interval` elapses, whichever first.

    The poll fallback is what makes a lost NOTIFY cost latency instead of
    correctness -- a queue with nothing pending simply wakes up on its own
    every `poll_interval` and finds nothing to claim.

    Args:
        event: The event to wait on; cleared on return either way
        poll_interval: Maximum time to wait before giving up on the NOTIFY
    """
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(event.wait(), timeout=poll_interval)
    event.clear()


class ReconciliationTimer:
    """
    Runs a callback on an interval, behind a Postgres advisory lock so that
    with more than one replica exactly one of them runs it at a time.

    Claiming is already replica-safe on its own (SKIP LOCKED, lease, unique
    dedup); this is for the things that are not -- lease reclaim here, and
    (for callers outside this package) a sweep controller or a settings
    cache invalidation.
    """

    def __init__(
        self, db: DatabaseConnection, lock_key: int,
        callback: Callable[[], Awaitable[None]], interval_seconds: float,
    ) -> None:
        """
        Args:
            db: Database connection to acquire the advisory lock on
            lock_key: Integer key for pg_try_advisory_lock -- callers must
                pick disjoint keys for different timers sharing a database
            callback: Coroutine run while holding the lock
            interval_seconds: Time between attempts
        """
        self._db = db
        self._lock_key = lock_key
        self._callback = callback
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the periodic loop."""
        self._task = asyncio.create_task(self._loop(), name="reconciliation-timer")

    async def stop(self) -> None:
        """Stop the periodic loop."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_once(self) -> bool:
        """
        Attempt the callback once, now, outside the periodic loop --
        exposed directly so a test or a manual trigger doesn't have to wait
        out `interval_seconds`.

        Returns:
            True if this call held the lock and ran the callback; False if
            another replica held it instead
        """
        async with self._db.engine.connect() as conn:
            lock_result = await conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": self._lock_key},
            )
            acquired = lock_result.scalar()
            if not acquired:
                return False
            try:
                await self._callback()
            finally:
                await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self._lock_key})
            return True

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Reconciliation callback raised")
            await asyncio.sleep(self._interval)
