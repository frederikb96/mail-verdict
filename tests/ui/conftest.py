"""
UI-layer fixtures: a real browser (Playwright, via pytest-playwright) driving
the MailVerdict app bound to an actual TCP port, against the same real
Postgres, PostIMAP, Dovecot, and Mailpit containers tests/e2e/ uses.

tests/e2e/ runs the app in-process against an ASGI TestClient -- no socket,
no browser can reach it. This layer needs the app actually listening, so
app_server here runs uvicorn for real, in a background thread, and hands
back the base URL. dovecot_endpoint and mailpit_http_url come from
tests.setup.containers (the pytest_plugins entry the root conftest
registers) rather than being redefined here, the same fixtures tests/e2e/
uses -- one definition for both layers.

Every test under tests/ui/ is auto-marked `ui` by the root conftest's
pytest_collection_modifyitems.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from testcontainers.core.container import DockerContainer

from mail_verdict.config.loader import reset_config
from tests.setup.migrations import run_migrations

_REPO_ROOT = Path(__file__).parent.parent.parent

_APP_READY_TIMEOUT_S = 30.0
_APP_SHUTDOWN_TIMEOUT_S = 10.0


def _assert_ui_build_is_fresh() -> None:
    """
    Fail loudly, naming the build command, rather than skip.

    A stale build is worse than a missing one -- the suite would pass
    against markup a source change never reached, silently. This project
    does not skip a test layer for a missing capability; a missing or
    outdated ui/build is exactly that kind of capability.
    """
    build_index = _REPO_ROOT / "ui" / "build" / "index.html"
    if not build_index.exists():
        raise RuntimeError(
            "tests/ui/ serves ui/build directly and it does not exist. Build it first:\n\n"
            "    cd ui && npm run build\n"
        )

    build_mtime = build_index.stat().st_mtime
    src_dir = _REPO_ROOT / "ui" / "src"
    stale = [p for p in src_dir.rglob("*") if p.is_file() and p.stat().st_mtime > build_mtime]
    if stale:
        newest = max(stale, key=lambda p: p.stat().st_mtime)
        raise RuntimeError(
            f"ui/build is older than {newest.relative_to(_REPO_ROOT)} -- rebuild it:\n\n"
            "    cd ui && npm run build\n"
        )


@pytest.fixture(scope="session", autouse=True)
def _ui_build_fresh() -> None:
    """Every test in this directory depends on this implicitly, via autouse."""
    _assert_ui_build_is_fresh()


class _ThreadedUvicornServer(uvicorn.Server):
    """A uvicorn server runnable in a background thread.

    Base uvicorn.Server.install_signal_handlers() calls signal.signal(),
    which raises outside the main thread -- overridden to a no-op since
    shutdown here is driven by should_exit, not a signal.
    """

    def install_signal_handlers(self) -> None:
        pass


@pytest.fixture(scope="module")
def app_server(
    postgres_url: str,
    dovecot_container: DockerContainer,
    mailpit_container: DockerContainer,
    postimap_container: DockerContainer,
) -> Iterator[str]:
    """
    The MailVerdict ASGI app, migrated and actually listening on a loopback
    port for one test module, reused by every test in that module -- the
    tests/e2e/ app_client pattern, with a real socket a browser can reach.

    Binding port 0 and reading back what the OS actually chose avoids the
    check-then-bind race a pre-picked free port would have.

    Migrations run inside their own worker thread, not a plain
    asyncio.run() on this one: pytest-playwright's sync API bridges to its
    own asyncio loop by making one appear "running" on the main thread for
    the duration of the browser/page fixtures, and asyncio.run() refuses
    to nest inside a loop that is already running.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, run_migrations(postgres_url)).result()

    os.environ["MAIL_VERDICT_DATABASE_URL"] = postgres_url
    reset_config()

    from mail_verdict.server import create_app

    app = create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = _ThreadedUvicornServer(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + _APP_READY_TIMEOUT_S
    while not server.started:
        if time.monotonic() > deadline:
            raise TimeoutError("uvicorn did not start within the timeout")
        time.sleep(0.05)

    port = server.servers[0].sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    deadline = time.monotonic() + _APP_READY_TIMEOUT_S
    last_status: int | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/api/health", timeout=2.0)
            last_status = resp.status_code
            if resp.status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    else:
        raise TimeoutError(f"App did not become ready within {_APP_READY_TIMEOUT_S}s "
                            f"(last /api/health status: {last_status})")

    yield base_url

    server.should_exit = True
    thread.join(timeout=_APP_SHUTDOWN_TIMEOUT_S)
    reset_config()


@pytest.fixture(scope="module")
def api_client(app_server: str) -> Iterator[httpx.Client]:
    """A plain HTTP client against the real, listening app -- for seeding
    and asserting state the browser itself never needs to see.

    A generous timeout: this shares the app's single event loop with real
    embedding/classification calls to a language model provider, which can
    make an individual request slow without anything being wrong.
    """
    with httpx.Client(base_url=app_server, timeout=30.0) as client:
        yield client
