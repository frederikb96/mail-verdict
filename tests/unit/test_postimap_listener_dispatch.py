"""
Bounded, queue-based NOTIFY dispatch: a burst of events is drained by a
fixed number of workers rather than one asyncio task per event, and a
queued event survives until a worker picks it up.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import pytest

from mail_verdict.postimap.listener import DISPATCH_CONCURRENCY, PostimapListener


@pytest.fixture()
def listener() -> PostimapListener:
    return PostimapListener("postgresql://user:pass@host/db")


def _payload(event_id: str) -> str:
    return json.dumps({
        "v": 1, "type": "message", "op": "insert", "id": event_id, "account_id": "a1",
    })


async def _stop(listener: PostimapListener, tasks: list[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestConcurrencyBound:
    @pytest.mark.asyncio
    async def test_never_exceeds_the_configured_bound(self, listener: PostimapListener) -> None:
        """A burst of events must never run more than DISPATCH_CONCURRENCY
        handler calls at once, however many are queued -- the unbounded
        fire-and-forget task this replaces had no such ceiling and could
        run thousands at once against a connection pool of roughly
        fifteen."""
        in_flight = 0
        max_in_flight = 0
        lock = asyncio.Lock()
        release = asyncio.Event()

        async def _handler(event: object) -> None:
            nonlocal in_flight, max_in_flight
            async with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            await release.wait()
            async with lock:
                in_flight -= 1

        listener.add_handler(_handler)
        dispatch_tasks = [
            asyncio.create_task(listener._dispatch_loop()) for _ in range(DISPATCH_CONCURRENCY)
        ]
        try:
            for i in range(DISPATCH_CONCURRENCY * 5):
                listener._on_notify(None, 0, "postimap_events", _payload(str(i)))

            # Long enough for every worker to have claimed an event and
            # blocked on release -- a regression back to unbounded tasks
            # would blow well past DISPATCH_CONCURRENCY immediately.
            await asyncio.sleep(0.2)
            assert max_in_flight == DISPATCH_CONCURRENCY
            assert in_flight == DISPATCH_CONCURRENCY
        finally:
            release.set()
            await asyncio.sleep(0.05)
            await _stop(listener, dispatch_tasks)


class TestQueueBehaviour:
    @pytest.mark.asyncio
    async def test_a_queued_event_still_reaches_its_handler(
        self, listener: PostimapListener,
    ) -> None:
        """The ordinary path: parse, queue, a worker picks it up and calls
        every registered handler."""
        received: list[str] = []

        async def _handler(event: object) -> None:
            received.append(event.id)  # type: ignore[attr-defined]

        listener.add_handler(_handler)
        dispatch_tasks = [asyncio.create_task(listener._dispatch_loop())]
        try:
            listener._on_notify(None, 0, "postimap_events", _payload("m1"))
            await asyncio.sleep(0.05)
            assert received == ["m1"]
        finally:
            await _stop(listener, dispatch_tasks)

    @pytest.mark.asyncio
    async def test_a_full_queue_drops_and_logs_rather_than_raising(
        self, listener: PostimapListener,
    ) -> None:
        """`_on_notify` is a synchronous callback invoked by asyncpg -- it
        cannot block waiting for room, so overflow must be a logged drop,
        never an unhandled exception that would take the callback (and
        thus every other channel listener on the same connection) down.

        Captures on the module's own logger rather than through the root,
        so the assertion holds whatever else in the process has
        reconfigured logging by the time this runs.
        """
        records: list[logging.LogRecord] = []
        module_logger = logging.getLogger("mail_verdict.postimap.listener")
        original_level = module_logger.level
        original_disabled = module_logger.disabled
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[method-assign]
        module_logger.addHandler(handler)
        module_logger.setLevel(logging.WARNING)
        # A dictConfig run anywhere in the process (uvicorn, fastmcp -- not
        # this module's own doing) with disable_existing_loggers=True marks
        # every already-created logger not named in its own config as
        # disabled, silently short-circuiting it regardless of level or
        # handlers. Restored in the finally below.
        module_logger.disabled = False
        try:
            listener._queue = asyncio.Queue(maxsize=1)
            listener._on_notify(None, 0, "postimap_events", _payload("first"))
            listener._on_notify(None, 0, "postimap_events", _payload("second"))
        finally:
            module_logger.removeHandler(handler)
            module_logger.setLevel(original_level)
            module_logger.disabled = original_disabled

        assert listener._queue.qsize() == 1
        assert any("dispatch queue full" in record.getMessage() for record in records)

    @pytest.mark.asyncio
    async def test_an_invalid_payload_is_never_queued(self, listener: PostimapListener) -> None:
        listener._on_notify(None, 0, "postimap_events", "not json")

        assert listener._queue.qsize() == 0
