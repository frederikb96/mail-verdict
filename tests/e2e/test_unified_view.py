"""
E2E test: Unified view API.

Tests multi-account folder merging, unified mail listing,
emoji assignment, and folder order management.
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import get_account_id

pytestmark = [pytest.mark.e2e]


async def _get_alice_id(client: httpx.AsyncClient) -> str:
    """Get alice's account ID."""
    return await get_account_id(client, name="alice")


async def _get_inbox_folder_id(
    client: httpx.AsyncClient,
    account_id: str,
) -> str:
    """Get INBOX folder ID for an account."""
    resp = await client.get(f"/api/accounts/{account_id}/folders")
    assert resp.status_code == 200
    inbox = next(f for f in resp.json() if f["imap_name"] == "INBOX")
    return inbox["id"]


# --- Emoji ---


@pytest.mark.asyncio
async def test_set_account_emoji(app_client: httpx.AsyncClient) -> None:
    """PUT /accounts/:id/emoji sets the account emoji."""
    account_id = await _get_alice_id(app_client)

    resp = await app_client.put(
        f"/api/accounts/{account_id}/emoji",
        json={"emoji": "A"},
    )
    assert resp.status_code == 200
    assert resp.json()["emoji"] == "A"

    # Verify persistence
    acct_resp = await app_client.get(f"/api/accounts/{account_id}")
    assert acct_resp.json()["emoji"] == "A"

    # Clear emoji
    await app_client.put(
        f"/api/accounts/{account_id}/emoji",
        json={"emoji": None},
    )


@pytest.mark.asyncio
async def test_clear_account_emoji(app_client: httpx.AsyncClient) -> None:
    """Setting emoji to null clears it."""
    account_id = await _get_alice_id(app_client)

    await app_client.put(
        f"/api/accounts/{account_id}/emoji",
        json={"emoji": "X"},
    )

    resp = await app_client.put(
        f"/api/accounts/{account_id}/emoji",
        json={"emoji": None},
    )
    assert resp.status_code == 200
    assert resp.json()["emoji"] is None


# --- Unified name ---


@pytest.mark.asyncio
async def test_set_folder_unified_name(
    app_client: httpx.AsyncClient,
) -> None:
    """Set a unified name on a folder for cross-account merging."""
    account_id = await _get_alice_id(app_client)
    folder_id = await _get_inbox_folder_id(app_client, account_id)

    resp = await app_client.put(
        f"/api/accounts/{account_id}/folders/{folder_id}/unified-name",
        json={"unified_name": "Inbox"},
    )
    assert resp.status_code == 200
    assert resp.json()["unified_name"] == "Inbox"

    # Clear
    await app_client.put(
        f"/api/accounts/{account_id}/folders/{folder_id}/unified-name",
        json={"unified_name": None},
    )


# --- Unified folders ---


@pytest.mark.asyncio
async def test_list_unified_folders_empty(
    app_client: httpx.AsyncClient,
) -> None:
    """Without unified names set, unified folders list is empty."""
    resp = await app_client.get("/api/unified/folders")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_unified_folders_after_setting_name(
    app_client: httpx.AsyncClient,
) -> None:
    """After setting a unified name, the folder appears in the unified list."""
    account_id = await _get_alice_id(app_client)
    folder_id = await _get_inbox_folder_id(app_client, account_id)

    # Set unified name
    await app_client.put(
        f"/api/accounts/{account_id}/folders/{folder_id}/unified-name",
        json={"unified_name": "TestInbox"},
    )

    # List unified folders
    resp = await app_client.get("/api/unified/folders")
    assert resp.status_code == 200
    folders = resp.json()
    names = [f["unified_name"] for f in folders]
    assert "TestInbox" in names

    # Find the unified folder and verify structure
    unified = next(f for f in folders if f["unified_name"] == "TestInbox")
    assert "folders" in unified
    assert "unread_count" in unified
    assert "total_count" in unified
    assert len(unified["folders"]) >= 1

    # Clean up
    await app_client.put(
        f"/api/accounts/{account_id}/folders/{folder_id}/unified-name",
        json={"unified_name": None},
    )


# --- Unified mails ---


@pytest.mark.asyncio
async def test_unified_mails_endpoint(
    app_client: httpx.AsyncClient,
) -> None:
    """Unified mails endpoint returns paginated response for a unified folder."""
    account_id = await _get_alice_id(app_client)
    folder_id = await _get_inbox_folder_id(app_client, account_id)

    # Set unified name
    await app_client.put(
        f"/api/accounts/{account_id}/folders/{folder_id}/unified-name",
        json={"unified_name": "TestMailView"},
    )

    resp = await app_client.get(
        "/api/unified/mails",
        params={"folder_name": "TestMailView", "limit": 5},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "mails" in data
    assert "has_more" in data
    assert isinstance(data["mails"], list)

    # Clean up
    await app_client.put(
        f"/api/accounts/{account_id}/folders/{folder_id}/unified-name",
        json={"unified_name": None},
    )


# --- Unified folder order ---


@pytest.mark.asyncio
async def test_get_unified_folder_order(
    app_client: httpx.AsyncClient,
) -> None:
    """GET /unified/folder-order returns order list."""
    resp = await app_client.get("/api/unified/folder-order")
    assert resp.status_code == 200
    data = resp.json()
    assert "order" in data
    assert isinstance(data["order"], list)


@pytest.mark.asyncio
async def test_set_unified_folder_order(
    app_client: httpx.AsyncClient,
) -> None:
    """PUT /unified/folder-order saves display order."""
    custom_order = ["Inbox", "Sent", "Archive"]
    resp = await app_client.put(
        "/api/unified/folder-order",
        json={"order": custom_order},
    )
    assert resp.status_code == 200
    assert resp.json()["order"] == custom_order

    # Verify persistence
    resp2 = await app_client.get("/api/unified/folder-order")
    assert resp2.json()["order"] == custom_order

    # Restore empty
    await app_client.put(
        "/api/unified/folder-order",
        json={"order": []},
    )
