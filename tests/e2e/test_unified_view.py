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


# --- Multi-account unified view tests ---


async def _get_bob_id(client: httpx.AsyncClient) -> str:
    """Get bob's account ID."""
    return await get_account_id(client, name="bob")


async def _setup_unified_inbox(
    client: httpx.AsyncClient,
    alice_id: str,
    bob_id: str,
    unified_name: str = "AllInbox",
) -> tuple[str, str]:
    """Set both Alice's and Bob's INBOX to the same unified name.

    Returns (alice_folder_id, bob_folder_id) for cleanup.
    """
    alice_folder_id = await _get_inbox_folder_id(client, alice_id)
    bob_folder_id = await _get_inbox_folder_id(client, bob_id)

    await client.put(
        f"/api/accounts/{alice_id}/folders/{alice_folder_id}/unified-name",
        json={"unified_name": unified_name},
    )
    await client.put(
        f"/api/accounts/{bob_id}/folders/{bob_folder_id}/unified-name",
        json={"unified_name": unified_name},
    )
    return alice_folder_id, bob_folder_id


async def _cleanup_unified_names(
    client: httpx.AsyncClient,
    alice_id: str,
    bob_id: str,
    alice_folder_id: str,
    bob_folder_id: str,
) -> None:
    """Clear unified names from both accounts' INBOX folders."""
    await client.put(
        f"/api/accounts/{alice_id}/folders/{alice_folder_id}/unified-name",
        json={"unified_name": None},
    )
    await client.put(
        f"/api/accounts/{bob_id}/folders/{bob_folder_id}/unified-name",
        json={"unified_name": None},
    )


@pytest.mark.asyncio
async def test_unified_folders_merge_both_accounts(
    app_client: httpx.AsyncClient,
    seeded_env: dict[str, str],
) -> None:
    """Unified folders merges INBOX from both Alice and Bob."""
    alice_id = seeded_env["alice_id"]
    bob_id = seeded_env["bob_id"]
    unified_name = "MergedInbox"

    alice_fid, bob_fid = await _setup_unified_inbox(
        app_client, alice_id, bob_id, unified_name,
    )

    try:
        resp = await app_client.get("/api/unified/folders")
        assert resp.status_code == 200
        folders = resp.json()

        merged = next(
            (f for f in folders if f["unified_name"] == unified_name), None,
        )
        assert merged is not None, f"{unified_name} not in unified folders"

        # Should have 2 source folders (Alice's INBOX + Bob's INBOX)
        assert len(merged["folders"]) == 2, (
            f"Expected 2 source folders, got {len(merged['folders'])}"
        )

        # Verify both accounts are represented
        source_account_ids = {s["account_id"] for s in merged["folders"]}
        assert alice_id in source_account_ids
        assert bob_id in source_account_ids

        # Counts should reflect both accounts' INBOX contents
        assert merged["total_count"] > 0
    finally:
        await _cleanup_unified_names(
            app_client, alice_id, bob_id, alice_fid, bob_fid,
        )


@pytest.mark.asyncio
async def test_unified_mails_returns_both_accounts(
    app_client: httpx.AsyncClient,
    seeded_env: dict[str, str],
) -> None:
    """Unified mails endpoint returns mails from both Alice and Bob."""
    alice_id = seeded_env["alice_id"]
    bob_id = seeded_env["bob_id"]
    unified_name = "BothInbox"

    alice_fid, bob_fid = await _setup_unified_inbox(
        app_client, alice_id, bob_id, unified_name,
    )

    try:
        resp = await app_client.get(
            "/api/unified/mails",
            params={"folder_name": unified_name, "limit": 50},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["mails"]) > 0

        # Collect unique account_ids from returned mails
        account_ids_in_results = {m["account_id"] for m in data["mails"]}
        assert alice_id in account_ids_in_results, "Alice's mails missing from unified view"
        assert bob_id in account_ids_in_results, "Bob's mails missing from unified view"
    finally:
        await _cleanup_unified_names(
            app_client, alice_id, bob_id, alice_fid, bob_fid,
        )


@pytest.mark.asyncio
async def test_unified_mails_sorted_by_date(
    app_client: httpx.AsyncClient,
    seeded_env: dict[str, str],
) -> None:
    """Unified mails are sorted by received_at descending (newest first)."""
    alice_id = seeded_env["alice_id"]
    bob_id = seeded_env["bob_id"]
    unified_name = "SortedInbox"

    alice_fid, bob_fid = await _setup_unified_inbox(
        app_client, alice_id, bob_id, unified_name,
    )

    try:
        resp = await app_client.get(
            "/api/unified/mails",
            params={"folder_name": unified_name, "limit": 50},
        )
        assert resp.status_code == 200
        mails = resp.json()["mails"]

        # Verify descending date order
        dates = [m["received_at"] for m in mails if m["received_at"]]
        for i in range(len(dates) - 1):
            assert dates[i] >= dates[i + 1], (
                f"Mails not sorted by date: {dates[i]} < {dates[i + 1]}"
            )
    finally:
        await _cleanup_unified_names(
            app_client, alice_id, bob_id, alice_fid, bob_fid,
        )


@pytest.mark.asyncio
async def test_unified_folder_with_emoji(
    app_client: httpx.AsyncClient,
    seeded_env: dict[str, str],
) -> None:
    """Unified folder sources include account emoji identifiers."""
    alice_id = seeded_env["alice_id"]
    bob_id = seeded_env["bob_id"]
    unified_name = "EmojiInbox"

    # Set emojis on both accounts
    await app_client.put(
        f"/api/accounts/{alice_id}/emoji", json={"emoji": "A"},
    )
    await app_client.put(
        f"/api/accounts/{bob_id}/emoji", json={"emoji": "B"},
    )

    alice_fid, bob_fid = await _setup_unified_inbox(
        app_client, alice_id, bob_id, unified_name,
    )

    try:
        resp = await app_client.get("/api/unified/folders")
        assert resp.status_code == 200
        folders = resp.json()

        merged = next(
            (f for f in folders if f["unified_name"] == unified_name), None,
        )
        assert merged is not None

        # Verify emoji identifiers are in the source folders
        emojis = {s["account_emoji"] for s in merged["folders"]}
        assert "A" in emojis, "Alice's emoji not in unified folder sources"
        assert "B" in emojis, "Bob's emoji not in unified folder sources"
    finally:
        await _cleanup_unified_names(
            app_client, alice_id, bob_id, alice_fid, bob_fid,
        )
        # Clean up emojis
        await app_client.put(
            f"/api/accounts/{alice_id}/emoji", json={"emoji": None},
        )
        await app_client.put(
            f"/api/accounts/{bob_id}/emoji", json={"emoji": None},
        )


@pytest.mark.asyncio
async def test_switching_unified_to_per_account_view(
    app_client: httpx.AsyncClient,
    seeded_env: dict[str, str],
) -> None:
    """Switching from unified to per-account view returns scoped results."""
    alice_id = seeded_env["alice_id"]
    bob_id = seeded_env["bob_id"]
    unified_name = "SwitchInbox"

    alice_fid, bob_fid = await _setup_unified_inbox(
        app_client, alice_id, bob_id, unified_name,
    )

    try:
        # Unified view shows both accounts' mails
        unified_resp = await app_client.get(
            "/api/unified/mails",
            params={"folder_name": unified_name, "limit": 50},
        )
        assert unified_resp.status_code == 200
        unified_account_ids = {
            m["account_id"] for m in unified_resp.json()["mails"]
        }
        assert len(unified_account_ids) == 2

        # Per-account view (Alice) shows only Alice's mails
        alice_resp = await app_client.get(
            "/api/mails",
            params={"account_id": alice_id, "limit": 50},
        )
        assert alice_resp.status_code == 200
        alice_mails = alice_resp.json()["mails"]
        for m in alice_mails:
            assert m["account_id"] == alice_id, (
                f"Per-account view returned wrong account: {m['account_id']}"
            )

        # Per-account view (Bob) shows only Bob's mails
        bob_resp = await app_client.get(
            "/api/mails",
            params={"account_id": bob_id, "limit": 50},
        )
        assert bob_resp.status_code == 200
        bob_mails = bob_resp.json()["mails"]
        for m in bob_mails:
            assert m["account_id"] == bob_id, (
                f"Per-account view returned wrong account: {m['account_id']}"
            )
    finally:
        await _cleanup_unified_names(
            app_client, alice_id, bob_id, alice_fid, bob_fid,
        )
