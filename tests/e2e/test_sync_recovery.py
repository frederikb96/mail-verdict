"""
E2E test: Sync recovery after disruption.

Tests that the sync engine recovers gracefully after app restart
and that accounts return to ACTIVE state with data intact.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.e2e.conftest import (
    APP_BASE_URL,
    get_account_id,
)

pytestmark = [pytest.mark.e2e]


async def _get_alice_id(client: httpx.AsyncClient) -> str:
    """Get alice's account ID."""
    return await get_account_id(client, name="alice")


async def _get_bob_id(client: httpx.AsyncClient) -> str:
    """Get bob's account ID."""
    return await get_account_id(client, name="bob")


async def _wait_healthy(client: httpx.AsyncClient, timeout: int = 120) -> None:
    """Poll health endpoint until healthy or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            resp = await client.get("/api/health")
            if resp.status_code == 200:
                return
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
            pass
        await asyncio.sleep(1)
    raise TimeoutError(f"App not healthy after {timeout}s")


async def _wait_for_state(
    client: httpx.AsyncClient,
    account_id: str,
    target_state: str,
    timeout: int = 60,
) -> str:
    """Poll account state until it reaches the target or ERROR."""
    deadline = asyncio.get_event_loop().time() + timeout
    last_state = ""
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/api/accounts/{account_id}")
        if resp.status_code == 200:
            last_state = resp.json().get("state", "")
            if last_state == target_state:
                return last_state
            if last_state == "error":
                return last_state
        await asyncio.sleep(1)
    raise TimeoutError(
        f"Account {account_id[:8]} did not reach {target_state} "
        f"within {timeout}s (last: {last_state})"
    )


@pytest.mark.asyncio
async def test_sync_resumes_after_restart(
    seeded_env: dict[str, str],
) -> None:
    """Sync resumes and accounts return to ACTIVE after app restart.

    Verifies:
    - App health endpoint responds after restart
    - Both Alice and Bob accounts reach ACTIVE state
    - Mails are still present and queryable
    """
    alice_id = seeded_env["alice_id"]
    bob_id = seeded_env["bob_id"]

    # Get mail counts before restart
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=transport, timeout=30.0,
    ) as client:
        await _wait_healthy(client)

        alice_resp = await client.get(
            "/api/mails", params={"account_id": alice_id, "limit": 200},
        )
        alice_count_before = len(alice_resp.json()["mails"])

        bob_resp = await client.get(
            "/api/mails", params={"account_id": bob_id, "limit": 200},
        )
        bob_count_before = len(bob_resp.json()["mails"])

    # Restart the container
    proc = await asyncio.create_subprocess_exec(
        "podman", "restart", "mv-app-test",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    # Poll for healthy immediately (no hardcoded sleep)

    # Reconnect with fresh transport after restart
    fresh_transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=fresh_transport, timeout=30.0,
    ) as client:
        await _wait_healthy(client, timeout=120)

        # Wait for both accounts to reach ACTIVE
        alice_state = await _wait_for_state(client, alice_id, "active")
        assert alice_state == "active", f"Alice stuck in: {alice_state}"

        bob_state = await _wait_for_state(client, bob_id, "active")
        assert bob_state == "active", f"Bob stuck in: {bob_state}"

        # Verify mails are still present
        alice_resp = await client.get(
            "/api/mails", params={"account_id": alice_id, "limit": 200},
        )
        assert alice_resp.status_code == 200
        alice_count_after = len(alice_resp.json()["mails"])
        assert alice_count_after >= alice_count_before, (
            f"Alice lost mails after restart: {alice_count_before} -> {alice_count_after}"
        )

        bob_resp = await client.get(
            "/api/mails", params={"account_id": bob_id, "limit": 200},
        )
        assert bob_resp.status_code == 200
        bob_count_after = len(bob_resp.json()["mails"])
        assert bob_count_after >= bob_count_before, (
            f"Bob lost mails after restart: {bob_count_before} -> {bob_count_after}"
        )


@pytest.mark.asyncio
async def test_sync_status_reports_active_after_recovery(
    seeded_env: dict[str, str],
) -> None:
    """Sync status endpoint reports is_syncing=True after recovery."""
    alice_id = seeded_env["alice_id"]

    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=transport, timeout=30.0,
    ) as client:
        await _wait_healthy(client)

        resp = await client.get(f"/api/accounts/{alice_id}/sync/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "active"
        assert data["is_active"] is True
        assert data["is_syncing"] is True
