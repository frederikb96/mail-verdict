"""
E2E test: Account state and connectivity.

Tests account state machine values and connection testing.
PostIMAP handles sync; MailVerdict only reads the state.
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import get_account_id

pytestmark = [pytest.mark.e2e]


async def _get_alice_id(client: httpx.AsyncClient) -> str:
    """Get alice's account ID."""
    return await get_account_id(client, name="alice")


@pytest.mark.asyncio
async def test_account_state_field(
    app_client: httpx.AsyncClient,
) -> None:
    """Account state is a valid PostIMAP state machine value."""
    account_id = await _get_alice_id(app_client)
    resp = await app_client.get(f"/api/accounts/{account_id}")
    assert resp.status_code == 200
    account = resp.json()
    valid_states = {"created", "syncing", "active", "error", "disabled"}
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
