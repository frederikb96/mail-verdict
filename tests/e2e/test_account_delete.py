"""
E2E test: Account deletion cascade.

Tests that deleting an account removes all associated data
(folders, mails, verdicts, tags) with no orphaned records.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from tests.e2e.conftest import (
    APP_BASE_URL,
    SPAMMER_EMAIL,
    SPAMMER_PASSWORD,
    send_email,
)

pytestmark = [pytest.mark.e2e]


async def _wait_healthy(client: httpx.AsyncClient, timeout: int = 60) -> None:
    """Poll health endpoint until healthy or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            resp = await client.get("/api/health")
            if resp.status_code == 200:
                return
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
            pass
        await asyncio.sleep(2)
    raise TimeoutError(f"App not healthy after {timeout}s")


async def _wait_for_state(
    client: httpx.AsyncClient,
    account_id: str,
    target_state: str,
    timeout: int = 180,
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
        await asyncio.sleep(5)
    raise TimeoutError(
        f"Account {account_id[:8]} did not reach {target_state} "
        f"within {timeout}s (last: {last_state})"
    )


@pytest.mark.asyncio
async def test_delete_account_cascade(
    seeded_env: dict[str, str],
) -> None:
    """Create a temp account, sync it, delete it, verify zero orphaned data.

    Uses the 'spammer' Stalwart account (already seeded) to avoid
    needing to create new Stalwart accounts.
    """
    # Send a test email to the spammer account so it has data to sync
    send_email(
        from_addr=SPAMMER_EMAIL,
        from_password=SPAMMER_PASSWORD,
        to_addr=SPAMMER_EMAIL,
        subject="Delete test self-mail",
        body="This mail should be cleaned up on account deletion.",
    )
    time.sleep(1)

    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=transport, timeout=30.0,
    ) as client:
        await _wait_healthy(client)

        # Create the spammer account in MailVerdict
        resp = await client.post("/api/accounts", json={
            "name": "spammer-delete-test",
            "imap_host": "stalwart",
            "imap_port": 1143,
            "imap_user": SPAMMER_EMAIL,
            "imap_password": SPAMMER_PASSWORD,
            "smtp_host": "stalwart",
            "smtp_port": 2525,
            "smtp_user": SPAMMER_EMAIL,
            "smtp_password": SPAMMER_PASSWORD,
            "spam_enabled": False,
        })
        assert resp.status_code == 201, f"Account creation failed: {resp.text}"
        account_id = resp.json()["id"]

    # Restart to pick up the new account for sync
    proc = await asyncio.create_subprocess_exec(
        "podman", "restart", "mv-app-test",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    await asyncio.sleep(15)

    fresh_transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=fresh_transport, timeout=30.0,
    ) as client:
        await _wait_healthy(client, timeout=120)

        # Wait for account to reach ACTIVE (synced)
        state = await _wait_for_state(client, account_id, "active")
        assert state == "active", f"Account stuck in: {state}"

        # Verify it has synced data (folders and mails)
        folders_resp = await client.get(f"/api/accounts/{account_id}/folders")
        assert folders_resp.status_code == 200
        folders = folders_resp.json()
        assert len(folders) > 0, "Account should have folders after sync"

        mails_resp = await client.get(
            "/api/mails", params={"account_id": account_id, "limit": 200},
        )
        assert mails_resp.status_code == 200
        mails = mails_resp.json()["mails"]
        assert len(mails) > 0, "Account should have mails after sync"

        mail_ids = [m["id"] for m in mails]

        # Delete the account
        del_resp = await client.delete(f"/api/accounts/{account_id}")
        assert del_resp.status_code == 204

        # Verify account is gone
        get_resp = await client.get(f"/api/accounts/{account_id}")
        assert get_resp.status_code == 404

        # Verify no orphaned mails
        for mid in mail_ids:
            mail_resp = await client.get(
                f"/api/mails/{mid}",
                params={"account_id": account_id},
            )
            assert mail_resp.status_code == 404, (
                f"Mail {mid} still exists after account deletion"
            )

        # Verify no orphaned folders (endpoint returns empty list or 404)
        folders_resp = await client.get(f"/api/accounts/{account_id}/folders")
        if folders_resp.status_code == 200:
            remaining_folders = folders_resp.json()
            assert len(remaining_folders) == 0, (
                f"Found {len(remaining_folders)} orphaned folders after deletion"
            )
        else:
            assert folders_resp.status_code == 404

        # Verify the other accounts are unaffected
        alice_resp = await client.get(f"/api/accounts/{seeded_env['alice_id']}")
        assert alice_resp.status_code == 200
        assert alice_resp.json()["state"] == "active"

        bob_resp = await client.get(f"/api/accounts/{seeded_env['bob_id']}")
        assert bob_resp.status_code == 200
        assert bob_resp.json()["state"] == "active"
