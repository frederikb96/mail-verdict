"""
Session-scoped container fixtures for the pg and e2e test layers.

All containers share one testcontainers Network so PostIMAP can reach
Postgres, Dovecot, and Mailpit by hostname the way it would in compose or
a Kubernetes Pod -- never by a host-mapped port.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator

import httpx
import pytest
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network

from tests.setup.images import DOVECOT_IMAGE, MAILPIT_IMAGE, POSTGRES_IMAGE, POSTIMAP_IMAGE
from tests.setup.runtime import bootstrap_container_runtime

POSTGRES_ALIAS = "postgres"
POSTGRES_DB = "postimap"
POSTGRES_USER = "postimap"
POSTGRES_PASSWORD = "postimap-test"  # noqa: S105 -- throwaway, container-local only

POSTIMAP_HEALTH_PORT = 8090
POSTIMAP_READY_TIMEOUT_S = 60

# Any username authenticates against this one shared password and gets its
# mailbox created on first login -- there is no account-provisioning API on
# this image, matching scripts/seed_dev.py's development-stack setup.
DOVECOT_ALIAS = "dovecot"
DOVECOT_IMAP_PORT = 31143
DOVECOT_LMTP_PORT = 31024
DOVECOT_PASSWORD = "e2e-test-password"  # noqa: S105 -- throwaway, container-local only
DOVECOT_READY_TIMEOUT_S = 60

MAILPIT_ALIAS = "mailpit"
MAILPIT_SMTP_PORT = 1025
MAILPIT_HTTP_PORT = 8025
MAILPIT_READY_TIMEOUT_S = 60


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


def _wait_for_http_ready(host: str, port: int, path: str, timeout_s: float, what: str) -> None:
    """Poll an HTTP endpoint until it reports 200 or the timeout expires."""
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"http://{host}:{port}{path}", timeout=2.0)
            if resp.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(1)
    raise TimeoutError(f"{what} did not become ready within {timeout_s}s: {last_error}")


def _wait_for_tcp_port(host: str, port: int, timeout_s: float, what: str) -> None:
    """Poll a raw TCP port until a connection succeeds or the timeout expires.

    Used for containers (Dovecot) with no HTTP healthcheck to probe instead.
    """
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return
        except OSError as exc:
            last_error = exc
        time.sleep(1)
    raise TimeoutError(f"{what} did not become ready within {timeout_s}s: {last_error}")


@pytest.fixture(scope="session")
def postimap_container(
    test_network: Network, postgres_container: PostgresContainer,
) -> Iterator[DockerContainer]:
    """
    A PostIMAP container on the shared network, migrated and healthy.

    Zero accounts configured at fixture-ready time (it needs no IMAP server
    to reach /readyz) -- the pg layer uses it exactly like that. The e2e
    layer additionally depends on dovecot_container and mailpit_container
    to give the accounts it creates somewhere real to sync against.

    POSTIMAP_IMAP_TLS_REJECT_UNAUTHORIZED=false is set unconditionally:
    Dovecot presents a self-signed certificate, so any account created
    against it would otherwise sit in `error` forever (the same setting
    compose.dev.yaml carries on its postimap service, for the same
    reason). Harmless for the pg layer, which never creates an account
    with real IMAP settings.
    """
    container = (
        DockerContainer(POSTIMAP_IMAGE)
        .with_network(test_network)
        .with_env("DB_PASSWORD", POSTGRES_PASSWORD)
        .with_env("POSTIMAP_DATABASE_HOST", POSTGRES_ALIAS)
        .with_env("POSTIMAP_DATABASE_PORT", "5432")
        .with_env("POSTIMAP_DATABASE_NAME", POSTGRES_DB)
        .with_env("POSTIMAP_DATABASE_USER", POSTGRES_USER)
        .with_env("POSTIMAP_IMAP_TLS_REJECT_UNAUTHORIZED", "false")
        .with_exposed_ports(POSTIMAP_HEALTH_PORT)
    )
    with container as postimap:
        host = postimap.get_container_host_ip()
        port = int(postimap.get_exposed_port(POSTIMAP_HEALTH_PORT))
        _wait_for_http_ready(host, port, "/readyz", POSTIMAP_READY_TIMEOUT_S, "PostIMAP")
        yield postimap


@pytest.fixture(scope="session")
def dovecot_container(test_network: Network) -> Iterator[DockerContainer]:
    """
    A throwaway Dovecot mail world, network-aliased as `dovecot`.

    Any username authenticates against DOVECOT_PASSWORD and gets its
    mailbox created on first login. IMAP (31143) is plain, no TLS; LMTP
    (31024) speaks implicit TLS -- see tests/setup/mail_delivery.py.
    """
    container = (
        DockerContainer(DOVECOT_IMAGE)
        .with_network(test_network)
        .with_network_aliases(DOVECOT_ALIAS)
        .with_env("USER_PASSWORD", DOVECOT_PASSWORD)
        .with_exposed_ports(DOVECOT_IMAP_PORT, DOVECOT_LMTP_PORT)
    )
    with container as dovecot:
        host = dovecot.get_container_host_ip()
        port = int(dovecot.get_exposed_port(DOVECOT_IMAP_PORT))
        _wait_for_tcp_port(host, port, DOVECOT_READY_TIMEOUT_S, "Dovecot")
        yield dovecot


@pytest.fixture(scope="session")
def mailpit_container(test_network: Network) -> Iterator[DockerContainer]:
    """
    A Mailpit SMTP sink with an HTTP API, network-aliased as `mailpit`.

    Accepts any SMTP AUTH so an account's smtp_user/smtp_password
    round-trips through a real exchange, and its HTTP API lets a test
    assert on what was actually received rather than mocking the send.
    """
    container = (
        DockerContainer(MAILPIT_IMAGE)
        .with_network(test_network)
        .with_network_aliases(MAILPIT_ALIAS)
        .with_env("MP_SMTP_AUTH_ACCEPT_ANY", "1")
        .with_env("MP_SMTP_AUTH_ALLOW_INSECURE", "1")
        .with_exposed_ports(MAILPIT_SMTP_PORT, MAILPIT_HTTP_PORT)
    )
    with container as mailpit:
        host = mailpit.get_container_host_ip()
        port = int(mailpit.get_exposed_port(MAILPIT_HTTP_PORT))
        _wait_for_http_ready(host, port, "/readyz", MAILPIT_READY_TIMEOUT_S, "Mailpit")
        yield mailpit


@pytest.fixture(scope="session")
def postgres_url(postgres_container: PostgresContainer) -> str:
    """asyncpg-format connection URL for the shared Postgres, from the host side."""
    host = postgres_container.get_container_host_ip()
    port = postgres_container.get_exposed_port(5432)
    return (
        f"postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
        f"@{host}:{port}/{POSTGRES_DB}"
    )
