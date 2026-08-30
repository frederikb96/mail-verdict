"""
WorkQueueNotifier's NOTIFY-based wakeup and ReconciliationTimer's advisory
lock -- both against a real Postgres, since neither has any meaning
without one.
"""

from __future__ import annotations

import asyncio

import pytest

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.postimap.listener import parse_dsn_from_sqlalchemy_url
from mail_verdict.queue.notify import ReconciliationTimer, WorkQueueNotifier, wait_for_work


class TestWorkQueueNotifier:
    @pytest.mark.asyncio
    async def test_notify_wakes_the_registered_event(
        self, migrated_db: DatabaseConnection, postgres_url: str,
    ) -> None:
        notifier = WorkQueueNotifier(parse_dsn_from_sqlalchemy_url(postgres_url))
        await notifier.start()
        try:
            event = notifier.event_for("pipeline")
            assert event.is_set() is False

            async with migrated_db.session() as session:
                await WorkQueueNotifier.notify(session, "pipeline")

            await asyncio.wait_for(event.wait(), timeout=5.0)
        finally:
            await notifier.stop()

    @pytest.mark.asyncio
    async def test_notify_for_a_different_queue_does_not_wake_this_one(
        self, migrated_db: DatabaseConnection, postgres_url: str,
    ) -> None:
        notifier = WorkQueueNotifier(parse_dsn_from_sqlalchemy_url(postgres_url))
        await notifier.start()
        try:
            pipeline_event = notifier.event_for("pipeline")

            async with migrated_db.session() as session:
                await WorkQueueNotifier.notify(session, "embedding")

            with pytest.raises(TimeoutError):
                await asyncio.wait_for(pipeline_event.wait(), timeout=0.5)
        finally:
            await notifier.stop()


class TestWaitForWork:
    @pytest.mark.asyncio
    async def test_returns_promptly_once_the_event_is_set(self) -> None:
        event = asyncio.Event()
        event.set()

        await asyncio.wait_for(wait_for_work(event, poll_interval=5.0), timeout=0.5)

        assert event.is_set() is False  # cleared on return

    @pytest.mark.asyncio
    async def test_falls_back_to_the_poll_interval(self) -> None:
        event = asyncio.Event()

        started = asyncio.get_event_loop().time()
        await wait_for_work(event, poll_interval=0.1)
        elapsed = asyncio.get_event_loop().time() - started

        assert elapsed >= 0.1


class TestReconciliationTimer:
    @pytest.mark.asyncio
    async def test_run_once_executes_the_callback(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        calls = 0

        async def callback() -> None:
            nonlocal calls
            calls += 1

        timer = ReconciliationTimer(
            migrated_db, lock_key=424242, callback=callback, interval_seconds=60,
        )

        ran = await timer.run_once()

        assert ran is True
        assert calls == 1

    @pytest.mark.asyncio
    async def test_only_one_of_two_concurrent_holders_runs_the_callback(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Two replicas racing the same advisory lock key -- exactly one of them
        runs the callback, standing in for the "only one replica reconciles"
        requirement."""
        calls = 0

        async def slow_callback() -> None:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.3)

        timer_a = ReconciliationTimer(
            migrated_db, lock_key=424243, callback=slow_callback, interval_seconds=60,
        )
        timer_b = ReconciliationTimer(
            migrated_db, lock_key=424243, callback=slow_callback, interval_seconds=60,
        )

        results = await asyncio.gather(timer_a.run_once(), timer_b.run_once())

        assert sorted(results) == [False, True]
        assert calls == 1

    @pytest.mark.asyncio
    async def test_different_lock_keys_do_not_contend(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Two timers with disjoint keys -- e.g. lease reclaim and a sweep
        controller -- never block each other."""
        calls = 0

        async def callback() -> None:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.1)

        timer_a = ReconciliationTimer(
            migrated_db, lock_key=424244, callback=callback, interval_seconds=60,
        )
        timer_b = ReconciliationTimer(
            migrated_db, lock_key=424245, callback=callback, interval_seconds=60,
        )

        results = await asyncio.gather(timer_a.run_once(), timer_b.run_once())

        assert results == [True, True]
        assert calls == 2
