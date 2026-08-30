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
from tests.setup.containers import DOVECOT_IMAP_PORT, DOVECOT_LMTP_PORT, MAILPIT_HTTP_PORT
from tests.setup.migrations import run_migrations


@pytest.fixture(scope="module")
def app_client(
    postgres_url: str,
    dovecot_container: DockerContainer,
    mailpit_container: DockerContainer,
    postimap_container: DockerContainer,
) -> Iterator[TestClient]:
    """
    The MailVerdict ASGI app, migrated and started in-process for one test
    module, reused by every test in that module.

    dovecot_container and mailpit_container are requested before
    postimap_container (fixtures of equal, independent scope come up in
    the order they're first requested) so both network aliases have
    strictly more time to propagate through the shared network's DNS
    before any account gets inserted and PostIMAP resolves them for real.

    TestClient's context manager drives the ASGI lifespan protocol (the
    same startup/shutdown server.py wires for uvicorn), so init_database,
    the settings service, and the postimap_events listener all come up for
    real -- no manual lifespan plumbing needed here.
    """
    asyncio.run(run_migrations(postgres_url))

    os.environ["MAIL_VERDICT_DATABASE_URL"] = postgres_url
    os.environ.pop("MAIL_VERDICT_API_KEY", None)  # auth disabled -- dev mode
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
    connection = DatabaseConnection(DatabaseConfig(url=postgres_url, pool_size=2, max_overflow=0))
    await connection.init()
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture(scope="module")
def dovecot_endpoint(dovecot_container: DockerContainer) -> tuple[str, int, int]:
    """Host-mapped (host, imap_port, lmtp_port) for connecting to Dovecot from the test process."""
    host = dovecot_container.get_container_host_ip()
    imap_port = int(dovecot_container.get_exposed_port(DOVECOT_IMAP_PORT))
    lmtp_port = int(dovecot_container.get_exposed_port(DOVECOT_LMTP_PORT))
    return host, imap_port, lmtp_port


@pytest.fixture(scope="module")
def mailpit_http_url(mailpit_container: DockerContainer) -> str:
    """Host-mapped base URL for Mailpit's HTTP API."""
    host = mailpit_container.get_container_host_ip()
    port = int(mailpit_container.get_exposed_port(MAILPIT_HTTP_PORT))
    return f"http://{host}:{port}"
