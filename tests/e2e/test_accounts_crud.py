"""
E2E test: Account CRUD via API.

Tests account creation, update, deletion, and test-connection.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = [pytest.mark.e2e]


@pytest.mark.asyncio
async def test_list_accounts(app_client: httpx.AsyncClient) -> None:
    """GET /api/accounts returns at least the seeded account."""
    resp = await app_client.get("/api/accounts")
    assert resp.status_code == 200
    accounts = resp.json()
    assert len(accounts) >= 1
    names = [a["name"] for a in accounts]
    assert "alice" in names, f"Expected 'alice' in accounts, got {names}"


@pytest.mark.asyncio
async def test_account_has_v2_fields(app_client: httpx.AsyncClient) -> None:
    """Account response includes v2 fields (state, spam_enabled)."""
    resp = await app_client.get("/api/accounts")
    assert resp.status_code == 200
    acct = resp.json()[0]
    assert "state" in acct
    assert "spam_enabled" in acct
    assert "sync_lookback_days" in acct
    assert "embedding_lookback_days" in acct


@pytest.mark.asyncio
async def test_create_and_delete_account(app_client: httpx.AsyncClient) -> None:
    """Create a temporary account then delete it."""
    resp = await app_client.post("/api/accounts", json={
        "name": "temp-test",
        "imap_host": "stalwart",
        "imap_port": 1143,
        "imap_user": "temp@test.local",
    })
    assert resp.status_code == 201
    acct_id = resp.json()["id"]
    assert resp.json()["state"] == "created"

    # Delete
    del_resp = await app_client.delete(f"/api/accounts/{acct_id}")
    assert del_resp.status_code == 204

    # Verify gone
    get_resp = await app_client.get(f"/api/accounts/{acct_id}")
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_update_account(app_client: httpx.AsyncClient) -> None:
    """Update account lookback days."""
    resp = await app_client.get("/api/accounts")
    acct_id = resp.json()[0]["id"]

    update_resp = await app_client.put(f"/api/accounts/{acct_id}", json={
        "sync_lookback_days": 90,
    })
    assert update_resp.status_code == 200
    assert update_resp.json()["sync_lookback_days"] == 90

    # Restore
    await app_client.put(f"/api/accounts/{acct_id}", json={
        "sync_lookback_days": 180,
    })


@pytest.mark.asyncio
async def test_account_passwords_not_exposed(app_client: httpx.AsyncClient) -> None:
    """Passwords should never appear in API responses."""
    resp = await app_client.get("/api/accounts")
    acct = resp.json()[0]
    assert "imap_password" not in acct
    assert "smtp_password" not in acct
