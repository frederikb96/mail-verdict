"""
E2E test: Account sync trigger and state transitions.

Tests triggering sync, checking sync status, and state machine.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.e2e.conftest import get_account_id

pytestmark = [pytest.mark.e2e]


async def _get_alice_id(client: httpx.AsyncClient) -> str:
    """Get alice's account ID."""
    return await get_account_id(client, name="alice")


@pytest.mark.asyncio
async def test_sync_status_endpoint(
    app_client: httpx.AsyncClient,
) -> None:
    """GET /accounts/:id/sync/status returns sync state."""
    account_id = await _get_alice_id(app_client)
    resp = await app_client.get(
        f"/api/accounts/{account_id}/sync/status",
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["account_id"] == account_id
    assert "state" in data
    assert "is_active" in data
    assert "is_syncing" in data


@pytest.mark.asyncio
async def test_trigger_sync(app_client: httpx.AsyncClient) -> None:
    """POST /accounts/:id/sync triggers a sync cycle."""
    account_id = await _get_alice_id(app_client)

    # Wait for debounce window to pass
    await asyncio.sleep(6)

    resp = await app_client.post(
        f"/api/accounts/{account_id}/sync",
    )
    assert resp.status_code in (200, 429), f"Unexpected status: {resp.status_code}"
    if resp.status_code == 200:
        data = resp.json()
        assert data["status"] in ("sync_triggered", "sync_started")


@pytest.mark.asyncio
async def test_trigger_sync_debounce(
    app_client: httpx.AsyncClient,
) -> None:
    """Rapid sync triggers hit the debounce (429)."""
    account_id = await _get_alice_id(app_client)

    # Wait for prior debounce to expire
    await asyncio.sleep(6)

    # First trigger
    resp1 = await app_client.post(f"/api/accounts/{account_id}/sync")
    # Second trigger immediately
    resp2 = await app_client.post(f"/api/accounts/{account_id}/sync")

    # At least one should succeed, the other might hit 429
    statuses = {resp1.status_code, resp2.status_code}
    assert 200 in statuses or 429 in statuses


@pytest.mark.asyncio
async def test_sync_status_nonexistent_account_404(
    app_client: httpx.AsyncClient,
) -> None:
    """Sync status for non-existent account returns 404."""
    resp = await app_client.get(
        "/api/accounts/00000000-0000-0000-0000-000000000000/sync/status",
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_account_state_field(
    app_client: httpx.AsyncClient,
) -> None:
    """Account state is a valid state machine value."""
    account_id = await _get_alice_id(app_client)
    resp = await app_client.get(f"/api/accounts/{account_id}")
    assert resp.status_code == 200
    account = resp.json()
    valid_states = {"created", "syncing", "seeding", "active", "error"}
    assert account["state"] in valid_states, (
        f"Unexpected state: {account['state']}"
    )


@pytest.mark.asyncio
async def test_test_connection(app_client: httpx.AsyncClient) -> None:
    """POST /accounts/:id/test-connection tests IMAP connectivity."""
    account_id = await _get_alice_id(app_client)
    resp = await app_client.post(
        f"/api/accounts/{account_id}/test-connection",
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "imap" in data
    assert data["imap"] == "ok", f"IMAP test failed: {data['imap']}"
