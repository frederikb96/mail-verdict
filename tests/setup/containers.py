"""
Session-scoped container fixtures for the pg and e2e test layers.

All containers share one testcontainers Network so PostIMAP can reach
Postgres (and, for e2e, Dovecot) by hostname the way it would in compose or
a Kubernetes Pod -- never by a host-mapped port.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx
import pytest
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network

from tests.setup.images import POSTGRES_IMAGE, POSTIMAP_IMAGE
from tests.setup.runtime import bootstrap_container_runtime

POSTGRES_ALIAS = "postgres"
POSTGRES_DB = "postimap"
POSTGRES_USER = "postimap"
POSTGRES_PASSWORD = "postimap-test"  # noqa: S105 -- throwaway, container-local only

POSTIMAP_HEALTH_PORT = 8090
POSTIMAP_READY_TIMEOUT_S = 60


@pytest.fixture(scope="session")
def _container_runtime() -> None:
    """
    Bootstrap DOCKER_HOST once per test session before any container starts.

    Deliberately NOT autouse: this module is registered as a plugin at the
    root conftest (pytest_plugins must live there, not in tests/pg/), so it
    is visible to every test session including pure `pytest tests/unit/`
    runs. Every container fixture below depends on this explicitly instead,
    so a unit-only run never probes for a runtime it doesn't need.
    """
    bootstrap_container_runtime()


@pytest.fixture(scope="session")
def test_network(_container_runtime: None) -> Iterator[Network]:
    """A shared Docker network for all containers in this test session."""
    with Network() as network:
        yield network


@pytest.fixture(scope="session")
def postgres_container(test_network: Network) -> Iterator[PostgresContainer]:
    """A Postgres container, network-aliased as `postgres`, migrated by nothing yet."""
    container = (
        PostgresContainer(
            POSTGRES_IMAGE, dbname=POSTGRES_DB,
            username=POSTGRES_USER, password=POSTGRES_PASSWORD,
        )
        .with_network(test_network)
        .with_network_aliases(POSTGRES_ALIAS)
    )
    with container as pg:
        yield pg


def _wait_for_postimap_ready(host: str, port: int) -> None:
    """Poll PostIMAP's /readyz until it reports 200 or the timeout expires."""
    deadline = time.monotonic() + POSTIMAP_READY_TIMEOUT_S
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"http://{host}:{port}/readyz", timeout=2.0)
            if resp.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(1)
    raise TimeoutError(
        f"PostIMAP did not become ready within {POSTIMAP_READY_TIMEOUT_S}s: {last_error}"
    )


@pytest.fixture(scope="session")
def postimap_container(
    test_network: Network, postgres_container: PostgresContainer,
) -> Iterator[DockerContainer]:
    """
    A PostIMAP container on the shared network, migrated and healthy with
    zero accounts configured (it needs no IMAP server to reach /readyz).
    """
    container = (
        DockerContainer(POSTIMAP_IMAGE)
        .with_network(test_network)
        .with_env("DB_PASSWORD", POSTGRES_PASSWORD)
        .with_env("POSTIMAP_DATABASE_HOST", POSTGRES_ALIAS)
        .with_env("POSTIMAP_DATABASE_PORT", "5432")
        .with_env("POSTIMAP_DATABASE_NAME", POSTGRES_DB)
        .with_env("POSTIMAP_DATABASE_USER", POSTGRES_USER)
        .with_exposed_ports(POSTIMAP_HEALTH_PORT)
    )
    with container as postimap:
        host = postimap.get_container_host_ip()
        port = int(postimap.get_exposed_port(POSTIMAP_HEALTH_PORT))
        _wait_for_postimap_ready(host, port)
        yield postimap


@pytest.fixture(scope="session")
def postgres_url(postgres_container: PostgresContainer) -> str:
    """asyncpg-format connection URL for the shared Postgres, from the host side."""
    host = postgres_container.get_container_host_ip()
    port = postgres_container.get_exposed_port(5432)
    return (
        f"postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
        f"@{host}:{port}/{POSTGRES_DB}"
    )
