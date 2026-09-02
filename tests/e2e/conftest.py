"""
e2e-layer fixtures: the application running in-process (ASGI, no bound
port) against real Postgres, PostIMAP, Dovecot, and Mailpit containers.

One app instance is shared per test module (see app_client below) so a
scenario file can hold several tests without paying container/app startup
more than once, per the platform's own "several tests per file" guidance.
Every test under tests/e2e/ is auto-marked `e2e` by the root conftest's
pytest_collection_modifyitems.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from starlette.testclient import TestClient
from testcontainers.core.container import DockerContainer

from mail_verdict.config.loader import DatabaseConfig, reset_config
from mail_verdict.database.connection import DatabaseConnection
from tests.setup.migrations import run_migrations


@pytest.fixture(scope="module")
def app_client(
    postgres_url: str,
    dovecot_container: DockerContainer,
    mailpit_container: DockerContainer,
    radicale_container: DockerContainer,
    postimap_container: DockerContainer,
) -> Iterator[TestClient]:
    """
    The MailVerdict ASGI app, migrated and started in-process for one test
    module, reused by every test in that module.

    dovecot_container, mailpit_container and radicale_container are
    requested before postimap_container (fixtures of equal, independent
    scope come up in the order they're first requested) so every network
    alias has strictly more time to propagate through the shared
    network's DNS before any account gets inserted and PostIMAP resolves
    them for real. Without this, whichever e2e module happens to run
    first builds postimap_container long before a later module's own
    fixtures first reach for radicale_container -- PostIMAP is already
    running by the time Radicale joins the network, and a DAV account
    created moments later can lose the DNS propagation race PostIMAP's
    own retries would otherwise paper over silently.

    TestClient's context manager drives the ASGI lifespan protocol (the
    same startup/shutdown server.py wires for uvicorn), so init_database,
    the settings service, and the postimap_events listener all come up for
    real -- no manual lifespan plumbing needed here.
    """
    asyncio.run(run_migrations(postgres_url))

    os.environ["MAIL_VERDICT_DATABASE_URL"] = postgres_url
    reset_config()

    from mail_verdict.server import create_app

    app = create_app()
    with TestClient(app) as client:
        yield client

    reset_config()


@pytest_asyncio.fixture()
async def db(postgres_url: str) -> AsyncIterator[DatabaseConnection]:
    """
    A standalone DatabaseConnection for test-side assertions and seeding.

    Deliberately not routed through the app's own global singleton
    (app_client's lifespan already occupies that slot for the whole
    module) -- this is its own engine/pool, the same pattern tests/pg's
    restricted_db fixture uses for the same reason.
    """
    connection = DatabaseConnection(
        DatabaseConfig(url=postgres_url, pool_size=2, max_overflow=0, reserved_for_requests=0)
    )
    await connection.init()
    try:
        yield connection
    finally:
        await connection.close()


# dovecot_endpoint, mailpit_http_url and radicale_endpoint come from
# tests.setup.containers, the same session-scoped plugin fixtures tests/ui/
# reuses -- see that module's docstring for why they live there rather than
# in this e2e-only conftest.
