"""
Unit tests for queue/supervisor.py -- pure asyncio, no database, since the
supervisor only manages tasks and never touches SQL itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from mail_verdict.queue.supervisor import WorkerSupervisor

_POLL_TIMEOUT_S = 2.0


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = _POLL_TIMEOUT_S) -> None:
    """Poll a zero-arg callable until it returns truthy or time out."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _idle_worker(worker_id: str, stop_event: asyncio.Event) -> None:
    """A worker body that does nothing but wait to be retired."""
    while not stop_event.is_set():
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_reaches_initial_target_on_start() -> None:
    """Starting a supervisor spawns workers up to its initial target without
    needing a separate set_target call."""
    supervisor = WorkerSupervisor(
        "test", _idle_worker, initial_concurrency=3, reconcile_interval_seconds=0.05,
    )
    await supervisor.start()
    try:
        await _wait_until(lambda: supervisor.actual == 3)
    finally:
        await supervisor.stop(drain_timeout=1.0)


@pytest.mark.asyncio
async def test_concurrency_increase_takes_effect_without_a_restart() -> None:
    """Raising the target while running spawns more workers -- no stop/start cycle,
    matching the requirement that concurrency changes need no restart."""
    supervisor = WorkerSupervisor(
        "test", _idle_worker, initial_concurrency=1, reconcile_interval_seconds=0.05,
    )
    await supervisor.start()
    try:
        await _wait_until(lambda: supervisor.actual == 1)
        supervisor.set_target(4)
        await _wait_until(lambda: supervisor.actual == 4)
    finally:
        await supervisor.stop(drain_timeout=1.0)


@pytest.mark.asyncio
async def test_concurrency_decrease_retires_workers_without_cancelling_them() -> None:
    """Lowering the target asks the excess workers to stop rather than cancelling
    their task -- the idle worker body only exits when it observes its own
    stop_event, so seeing actual drop proves the event was set, not that the
    task was torn down mid-flight."""
    supervisor = WorkerSupervisor(
        "test", _idle_worker, initial_concurrency=4, reconcile_interval_seconds=0.05,
    )
    await supervisor.start()
    try:
        await _wait_until(lambda: supervisor.actual == 4)
        supervisor.set_target(1)
        await _wait_until(lambda: supervisor.actual == 1)
    finally:
        await supervisor.stop(drain_timeout=1.0)


@pytest.mark.asyncio
async def test_stop_releases_every_worker() -> None:
    """A clean shutdown waits for every worker to exit -- actual is 0 once stop()
    returns, not merely eventually."""
    supervisor = WorkerSupervisor(
        "test", _idle_worker, initial_concurrency=3, reconcile_interval_seconds=0.05,
    )
    await supervisor.start()
    await _wait_until(lambda: supervisor.actual == 3)

    await supervisor.stop(drain_timeout=1.0)

    assert supervisor.actual == 0


@pytest.mark.asyncio
async def test_a_crashed_worker_is_respawned() -> None:
    """A worker body raising is logged and the task ends -- the next reconcile tick
    sees the deficit and spawns a replacement, so one bad iteration doesn't
    quietly shrink the pool forever."""
    attempts = 0

    async def _flaky_worker(worker_id: str, stop_event: asyncio.Event) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")
        while not stop_event.is_set():
            await asyncio.sleep(0.01)

    supervisor = WorkerSupervisor(
        "test", _flaky_worker, initial_concurrency=1, reconcile_interval_seconds=0.05,
    )
    await supervisor.start()
    try:
        await _wait_until(lambda: attempts >= 2)
        await _wait_until(lambda: supervisor.actual == 1)
    finally:
        await supervisor.stop(drain_timeout=1.0)
