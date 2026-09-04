"""
Liveness must answer even while the async event loop the rest of the app
runs on is blocked -- a handler that never awaits (long synchronous work
inside an `async def`) blocks every request on that loop, the readiness
check included, for as long as it runs.

This drives the real ASGI app through a real uvicorn instance on a real
socket, with a test-only route reproducing that block, and observes both
ports from a plain synchronous client on the test's own thread -- neither
the uvicorn event loop nor the thread issuing the blocking request. That
third thread is what makes this a real test: `AsyncClient.get()` awaited
from a coroutine on the loop under test cannot even start running until
the block clears, so it always measures an unblocked call and always
passes, whatever is under test.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from starlette.responses import PlainTextResponse
from starlette.routing import Route

os.environ.setdefault(
    "MAIL_VERDICT_DATABASE_URL", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"
)

_BLOCK_SECONDS = 2.0


def _free_port() -> int:
    """An OS-assigned free TCP port, released immediately for the caller to bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _RunningApp:
    def __init__(self, app_port: int, liveness_port: int, block_started: threading.Event) -> None:
        self.app_port = app_port
        self.liveness_port = liveness_port
        self.block_started = block_started

    def fire_blocking_request(self) -> None:
        """Start a request that occupies the app's event loop for
        `_BLOCK_SECONDS`, from a thread of its own so this call returns
        immediately -- the caller waits on `block_started` instead."""

        def _fire() -> None:
            httpx.get(
                f"http://127.0.0.1:{self.app_port}/test-block-event-loop",
                timeout=_BLOCK_SECONDS + 5.0,
            )

        threading.Thread(target=_fire, daemon=True).start()
        assert self.block_started.wait(timeout=5.0), "blocking route never started"


@pytest.fixture()
def running_app() -> Iterator[_RunningApp]:
    """
    The real `create_app()` ASGI app, served by a real uvicorn instance in
    its own thread, plus the liveness listener started exactly the way
    `lifespan()` starts it -- called directly rather than through the full
    lifespan, since nothing here needs the database lifespan otherwise
    requires.

    Lifespan is off on the uvicorn side for the same reason: this fixture
    never runs `lifespan()` itself.
    """
    from mail_verdict.server import create_app, start_liveness_server, stop_liveness_server

    app_port = _free_port()
    liveness_port = _free_port()

    liveness_server, liveness_thread = start_liveness_server("127.0.0.1", liveness_port)

    app = create_app()
    block_started = threading.Event()

    async def _block_event_loop(request: object) -> PlainTextResponse:
        """Reproduces "long synchronous work inside an async handler"
        without needing the calendar code that originally did it."""
        block_started.set()
        time.sleep(_BLOCK_SECONDS)
        return PlainTextResponse("done")

    app.router.routes.insert(0, Route("/test-block-event-loop", _block_event_loop))

    config = uvicorn.Config(app, host="127.0.0.1", port=app_port, lifespan="off", log_level="error")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    deadline = time.monotonic() + 5.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn never reported ready"

    try:
        yield _RunningApp(app_port, liveness_port, block_started)
    finally:
        server.should_exit = True
        server_thread.join(timeout=5.0)
        stop_liveness_server(liveness_server, liveness_thread)


def test_liveness_answers_while_the_event_loop_is_blocked(running_app: _RunningApp) -> None:
    running_app.fire_blocking_request()

    # Control: the app's own port is genuinely stuck behind the block --
    # confirms the scenario is real, not just that the liveness port answers.
    with pytest.raises(httpx.TimeoutException):
        httpx.get(f"http://127.0.0.1:{running_app.app_port}/api/health", timeout=0.3)

    start = time.monotonic()
    resp = httpx.get(f"http://127.0.0.1:{running_app.liveness_port}/", timeout=1.0)
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    assert elapsed < 0.3, f"liveness took {elapsed:.2f}s while the event loop was blocked"
