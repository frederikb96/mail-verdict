"""
E2E test: Folder management API.

Tests folder ordering, visibility toggle, IDLE configuration,
and folder mapping.
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import get_account_id

pytestmark = [pytest.mark.e2e]


async def _get_alice_id(client: httpx.AsyncClient) -> str:
    """Get alice's account ID."""
    return await get_account_id(client, name="alice")


async def _get_folders(client: httpx.AsyncClient, account_id: str) -> list:
    """Fetch all folders for an account."""
    resp = await client.get(f"/api/accounts/{account_id}/folders")
    assert resp.status_code == 200
    return resp.json()


# --- Folder listing ---


@pytest.mark.asyncio
async def test_list_folders_has_standard_folders(
    app_client: httpx.AsyncClient,
) -> None:
    """Account has standard IMAP folders after sync."""
    account_id = await _get_alice_id(app_client)
    folders = await _get_folders(app_client, account_id)
    folder_names = [f["imap_name"] for f in folders]
    assert "INBOX" in folder_names
    assert any("Sent" in n for n in folder_names), f"No Sent folder in {folder_names}"
    assert any("Deleted" in n or "Trash" in n for n in folder_names)


@pytest.mark.asyncio
async def test_folder_has_expected_fields(
    app_client: httpx.AsyncClient,
) -> None:
    """Each folder response has the required fields."""
    account_id = await _get_alice_id(app_client)
    folders = await _get_folders(app_client, account_id)
    assert len(folders) > 0

    inbox = next(f for f in folders if f["imap_name"] == "INBOX")
    assert "id" in inbox
    assert "account_id" in inbox
    assert "special_use" in inbox
    assert inbox["special_use"] == "inbox"
    assert "unread_count" in inbox
    assert "total_count" in inbox
    assert "is_visible" in inbox
    assert isinstance(inbox["unread_count"], int)
    assert isinstance(inbox["total_count"], int)


# --- Folder order ---


@pytest.mark.asyncio
async def test_get_folder_order(app_client: httpx.AsyncClient) -> None:
    """GET /accounts/:id/folder-order returns ordered folder list."""
    account_id = await _get_alice_id(app_client)
    resp = await app_client.get(f"/api/accounts/{account_id}/folder-order")
    assert resp.status_code == 200
    data = resp.json()
    assert "folders" in data
    assert isinstance(data["folders"], list)
    assert len(data["folders"]) > 0


@pytest.mark.asyncio
async def test_update_folder_order(app_client: httpx.AsyncClient) -> None:
    """PUT /accounts/:id/folder-order saves custom order."""
    account_id = await _get_alice_id(app_client)

    # Get current folders
    resp = await app_client.get(f"/api/accounts/{account_id}/folder-order")
    current = resp.json()["folders"]
    original_ids = [f["folder_id"] for f in current]

    # Reverse the order
    reversed_ids = list(reversed(original_ids))
    resp = await app_client.put(
        f"/api/accounts/{account_id}/folder-order",
        json={"order": reversed_ids},
    )
    assert resp.status_code == 200
    updated = resp.json()["folders"]
    updated_ids = [f["folder_id"] for f in updated]
    assert updated_ids == reversed_ids

    # Restore original order
    await app_client.put(
        f"/api/accounts/{account_id}/folder-order",
        json={"order": original_ids},
    )


# --- Folder visibility ---


@pytest.mark.asyncio
async def test_toggle_folder_visibility(
    app_client: httpx.AsyncClient,
) -> None:
    """PATCH /accounts/:id/folders/:fid/visibility toggles visibility."""
    account_id = await _get_alice_id(app_client)
    folders = await _get_folders(app_client, account_id)

    # Pick a non-INBOX folder to toggle
    target = next(
        (f for f in folders if f["imap_name"] != "INBOX"),
        None,
    )
    assert target is not None, "Need at least one non-INBOX folder"
    folder_id = target["id"]
    original_visible = target["is_visible"]

    # Toggle to hidden
    resp = await app_client.patch(
        f"/api/accounts/{account_id}/folders/{folder_id}/visibility",
        json={"is_visible": not original_visible},
    )
    assert resp.status_code == 200
    assert resp.json()["is_visible"] is not original_visible

    # Restore
    await app_client.patch(
        f"/api/accounts/{account_id}/folders/{folder_id}/visibility",
        json={"is_visible": original_visible},
    )


# --- Folder mapping ---


@pytest.mark.asyncio
async def test_get_folder_mapping(app_client: httpx.AsyncClient) -> None:
    """GET /accounts/:id/folder-mapping returns mapping dict."""
    account_id = await _get_alice_id(app_client)
    resp = await app_client.get(f"/api/accounts/{account_id}/folder-mapping")
    assert resp.status_code == 200
    mapping = resp.json()
    assert isinstance(mapping, dict)
    # Should have at least inbox auto-detected
    assert "inbox" in mapping, f"Missing 'inbox' in mapping. Got: {list(mapping.keys())}"
    assert len(mapping) >= 1


@pytest.mark.asyncio
async def test_auto_detect_folder_mapping(
    app_client: httpx.AsyncClient,
) -> None:
    """POST /accounts/:id/folder-mapping/auto-detect re-runs detection."""
    account_id = await _get_alice_id(app_client)
    resp = await app_client.post(
        f"/api/accounts/{account_id}/folder-mapping/auto-detect",
    )
    assert resp.status_code == 200
    mapping = resp.json()
    assert isinstance(mapping, dict)
    assert "inbox" in mapping


@pytest.mark.asyncio
async def test_update_folder_mapping(
    app_client: httpx.AsyncClient,
) -> None:
    """PUT /accounts/:id/folder-mapping saves custom mapping."""
    account_id = await _get_alice_id(app_client)

    # Save custom mapping
    custom = {"inbox": "INBOX", "sent": "Sent Items", "trash": "Deleted Items"}
    resp = await app_client.put(
        f"/api/accounts/{account_id}/folder-mapping",
        json=custom,
    )
    assert resp.status_code == 200
    assert resp.json()["inbox"] == "INBOX"

    # Verify persistence
    resp2 = await app_client.get(f"/api/accounts/{account_id}/folder-mapping")
    assert resp2.json()["inbox"] == "INBOX"


# --- IDLE folders ---


@pytest.mark.asyncio
async def test_get_idle_folders(app_client: httpx.AsyncClient) -> None:
    """GET /accounts/:id/idle-folders lists all folders with IDLE status."""
    account_id = await _get_alice_id(app_client)
    resp = await app_client.get(f"/api/accounts/{account_id}/idle-folders")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) > 0
    assert "folder_id" in data[0]
    assert "imap_name" in data[0]
    assert "idle_enabled" in data[0]


@pytest.mark.asyncio
async def test_toggle_idle_folder(app_client: httpx.AsyncClient) -> None:
    """PUT /accounts/:id/idle-folders toggles IDLE for a specific folder."""
    account_id = await _get_alice_id(app_client)
    resp = await app_client.get(f"/api/accounts/{account_id}/idle-folders")
    folders = resp.json()
    inbox = next(f for f in folders if f["imap_name"] == "INBOX")
    folder_id = inbox["folder_id"]

    # Enable IDLE
    resp = await app_client.put(
        f"/api/accounts/{account_id}/idle-folders",
        json={"folder_id": folder_id, "enabled": True},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert resp.json()["enabled"] is True

    # Disable IDLE
    resp = await app_client.put(
        f"/api/accounts/{account_id}/idle-folders",
        json={"folder_id": folder_id, "enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
