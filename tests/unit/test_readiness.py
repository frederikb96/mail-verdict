"""Readiness (/api/health) re-checks the PostIMAP contract instead of latching false.

`_contract_ok` is set once during lifespan startup; if PostIMAP hasn't
migrated yet at that point, the probe must keep retrying rather than
reporting not-ready for the pod's entire lifetime -- the normal case when
installing both charts together.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault(
    "MAIL_VERDICT_DATABASE_URL", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"
)


@pytest.fixture()
def client() -> TestClient:
    """A TestClient over the real app, without running the DB-dependent lifespan.

    get_db_connection and _check_contract are patched per-test, so neither
    the database nor PostIMAP's own migrations need to exist here.
    """
    import mail_verdict.server as server_module

    return TestClient(server_module.create_app())


def test_readiness_recovers_once_the_contract_is_confirmed(client: TestClient) -> None:
    """A false _contract_ok from startup must not be permanent."""
    import mail_verdict.server as server_module

    server_module._contract_ok = False
    db = MagicMock()
    db.health_check = AsyncMock(return_value=True)

    with (
        patch.object(server_module, "get_db_connection", return_value=db),
        patch.object(
            server_module, "_check_contract", new=AsyncMock(return_value=True)
        ) as mocked_check,
    ):
        resp = client.get("/api/health")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
    mocked_check.assert_awaited_once_with(db)
    assert server_module._contract_ok is True


def test_readiness_keeps_retrying_while_the_contract_is_missing(
    client: TestClient,
) -> None:
    """Reporting not-ready must not stop the probe from re-checking next time."""
    import mail_verdict.server as server_module

    server_module._contract_ok = False
    db = MagicMock()
    db.health_check = AsyncMock(return_value=True)

    with (
        patch.object(server_module, "get_db_connection", return_value=db),
        patch.object(
            server_module, "_check_contract", new=AsyncMock(return_value=False)
        ) as mocked_check,
    ):
        first = client.get("/api/health")
        second = client.get("/api/health")

    assert first.status_code == 503
    assert second.status_code == 503
    assert mocked_check.await_count == 2


def test_readiness_skips_the_contract_check_once_confirmed(client: TestClient) -> None:
    """Once true, the contract check is not re-run on every probe (it's confirmed for good)."""
    import mail_verdict.server as server_module

    server_module._contract_ok = True
    db = MagicMock()
    db.health_check = AsyncMock(return_value=True)

    with (
        patch.object(server_module, "get_db_connection", return_value=db),
        patch.object(
            server_module, "_check_contract", new=AsyncMock(return_value=True)
        ) as mocked_check,
    ):
        resp = client.get("/api/health")

    assert resp.status_code == 200
    mocked_check.assert_not_awaited()


def test_readiness_leaves_the_service_when_the_database_is_unreachable(
    client: TestClient,
) -> None:
    """A confirmed contract is not enough: a pod that cannot reach its database
    can serve nothing but errors, so it must stop taking traffic."""
    import mail_verdict.server as server_module

    server_module._contract_ok = True
    db = MagicMock()
    db.health_check = AsyncMock(return_value=False)

    with patch.object(server_module, "get_db_connection", return_value=db):
        resp = client.get("/api/health")

    assert resp.status_code == 503
    assert resp.json()["database"] == "unreachable"


def test_readiness_treats_a_slow_database_as_busy_rather_than_broken(
    client: TestClient,
) -> None:
    """A loaded pod stays in the Service. At one replica, dropping out on
    latency alone takes the whole service down for being busy."""
    import asyncio

    import mail_verdict.server as server_module

    async def _never() -> bool:
        await asyncio.sleep(3600)
        return True

    server_module._contract_ok = True
    db = MagicMock()
    db.health_check = _never

    with patch.object(server_module, "get_db_connection", return_value=db):
        resp = client.get("/api/health")

    assert resp.status_code == 200
    assert resp.json()["database"] == "slow"
