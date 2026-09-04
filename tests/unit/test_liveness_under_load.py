"""
Test that liveness probe responds promptly even when event loop is blocked.

The event loop can be blocked by long synchronous work in a handler.
Liveness should still respond quickly because it must be independent
of the event loop's availability.
"""

from __future__ import annotations

import asyncio
import time

import pytest


@pytest.mark.asyncio
async def test_liveness_responsive_when_event_loop_blocked() -> None:
    """
    Liveness must respond quickly even when the event loop is blocked
    by long synchronous work in another handler.

    This test documents the expected behavior: liveness should not depend
    on the event loop being responsive, so it can answer even when the loop
    is busy with CPU-bound work.

    Current implementation: fails (liveness blocks with event loop)
    Expected after fix: passes (liveness responds from separate mechanism)
    """
    import threading

    from httpx import ASGITransport, AsyncClient
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    event_started = threading.Event()

    async def slow_endpoint(request):
        """Endpoint that blocks the event loop with CPU-bound work."""
        # Signal that we've started blocking
        event_started.set()
        # Do CPU-bound work that will block the entire event loop
        # This simulates the calendar endpoint doing heavy processing
        time.sleep(3)
        return JSONResponse({"status": "done"})

    async def health_live(request):
        """Liveness check - must respond regardless of event loop state."""
        return JSONResponse({"status": "alive"})

    app = Starlette(
        routes=[
            Route("/slow", slow_endpoint),
            Route("/api/health/live", health_live),
        ]
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Fire off the slow request which will block the event loop
        slow_task = asyncio.create_task(client.get("/slow", timeout=10.0))

        # Wait for it to actually start blocking
        await asyncio.sleep(0.05)
        assert event_started.is_set(), "Slow endpoint did not start"

        # Now try liveness while the event loop is truly blocked
        # If liveness depends on the event loop, this will be very slow
        start = time.time()
        try:
            live_response = await asyncio.wait_for(
                client.get("/api/health/live", timeout=2.0),
                timeout=2.0
            )
            elapsed = time.time() - start

            # REQUIREMENT: liveness must respond in under 100ms even when
            # the event loop is blocked by another request
            assert elapsed < 0.1, (
                f"Liveness took {elapsed:.2f}s - blocked on event loop, "
                f"needs independent mechanism"
            )
            assert live_response.status_code == 200
        except asyncio.TimeoutError:
            pytest.fail(
                "Liveness probe timed out while event loop was blocked. "
                "Liveness must not depend on event loop availability."
            )
        finally:
            slow_task.cancel()
            try:
                await slow_task
            except asyncio.CancelledError:
                pass
