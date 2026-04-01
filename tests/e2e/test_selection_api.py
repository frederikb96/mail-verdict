"""
E2E test: Selection API for multi-select operations.

Tests toggle, range, select-all, clear, and bulk actions
on the per-account selection state.
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import get_account_id

pytestmark = [pytest.mark.e2e]


async def _get_alice_id(client: httpx.AsyncClient) -> str:
    """Get alice's account ID."""
    return await get_account_id(client, name="alice")


async def _get_mail_ids(
    client: httpx.AsyncClient,
    account_id: str,
    limit: int = 10,
) -> list[str]:
    """Fetch mail IDs for an account."""
    resp = await client.get(
        "/api/mails",
        params={"account_id": account_id, "limit": limit},
    )
    assert resp.status_code == 200
    return [m["id"] for m in resp.json()["messages"]]


async def _get_inbox_folder_id(
    client: httpx.AsyncClient,
    account_id: str,
) -> str:
    """Get the INBOX folder ID for an account."""
    resp = await client.get(f"/api/accounts/{account_id}/folders")
    assert resp.status_code == 200
    folders = resp.json()
    inbox = next(f for f in folders if f["imap_name"] == "INBOX")
    return inbox["id"]


@pytest.mark.asyncio
async def test_get_selection_initially_empty(
    app_client: httpx.AsyncClient,
) -> None:
    """GET /accounts/:id/selection returns empty selection."""
    account_id = await _get_alice_id(app_client)

    # Clear first
    await app_client.post(f"/api/accounts/{account_id}/selection/clear")

    resp = await app_client.get(f"/api/accounts/{account_id}/selection")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["selected_ids"] == []


@pytest.mark.asyncio
async def test_toggle_selection(app_client: httpx.AsyncClient) -> None:
    """Toggle a mail into selection, then toggle it out."""
    account_id = await _get_alice_id(app_client)
    mail_ids = await _get_mail_ids(app_client, account_id)
    assert len(mail_ids) > 0
    target_id = mail_ids[0]

    # Clear first
    await app_client.post(f"/api/accounts/{account_id}/selection/clear")

    # Toggle ON
    resp = await app_client.post(
        f"/api/accounts/{account_id}/selection/toggle",
        json={"mail_id": target_id},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert target_id in data["selected_ids"]

    # Toggle OFF
    resp = await app_client.post(
        f"/api/accounts/{account_id}/selection/toggle",
        json={"mail_id": target_id},
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


@pytest.mark.asyncio
async def test_select_all_in_folder(
    app_client: httpx.AsyncClient,
) -> None:
    """Select all mails in INBOX and verify count matches folder total."""
    account_id = await _get_alice_id(app_client)
    folder_id = await _get_inbox_folder_id(app_client, account_id)

    # Select all
    resp = await app_client.post(
        f"/api/accounts/{account_id}/selection/all",
        json={"folder_id": folder_id},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] > 0

    # Verify count roughly matches INBOX
    folders_resp = await app_client.get(f"/api/accounts/{account_id}/folders")
    inbox = next(f for f in folders_resp.json() if f["imap_name"] == "INBOX")
    assert data["count"] == inbox["total_count"]

    # Clean up
    await app_client.post(f"/api/accounts/{account_id}/selection/clear")


@pytest.mark.asyncio
async def test_clear_selection(app_client: httpx.AsyncClient) -> None:
    """Clear removes all selections."""
    account_id = await _get_alice_id(app_client)
    mail_ids = await _get_mail_ids(app_client, account_id)

    # Select a few
    for mid in mail_ids[:3]:
        await app_client.post(
            f"/api/accounts/{account_id}/selection/toggle",
            json={"mail_id": mid},
        )

    # Clear
    resp = await app_client.post(
        f"/api/accounts/{account_id}/selection/clear",
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert resp.json()["selected_ids"] == []


@pytest.mark.asyncio
async def test_range_selection(app_client: httpx.AsyncClient) -> None:
    """Range selection picks up all mails between two anchors."""
    account_id = await _get_alice_id(app_client)
    folder_id = await _get_inbox_folder_id(app_client, account_id)

    # Get mails specifically in the INBOX folder
    resp = await app_client.get(
        "/api/mails",
        params={"account_id": account_id, "folder_id": folder_id, "limit": 20},
    )
    assert resp.status_code == 200
    inbox_mails = resp.json()["messages"]

    if len(inbox_mails) < 3:
        pytest.skip("Need at least 3 mails in INBOX for range selection test")

    # Clear first
    await app_client.post(f"/api/accounts/{account_id}/selection/clear")

    # Range select from first to third mail in INBOX
    resp = await app_client.post(
        f"/api/accounts/{account_id}/selection/range",
        json={
            "from_id": inbox_mails[0]["id"],
            "to_id": inbox_mails[2]["id"],
            "folder_id": folder_id,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    # Should have at least the two anchor mails
    assert data["count"] >= 2

    # Clean up
    await app_client.post(f"/api/accounts/{account_id}/selection/clear")


@pytest.mark.asyncio
async def test_bulk_action_mark_read(
    app_client: httpx.AsyncClient,
) -> None:
    """Bulk mark_read action on selected mails."""
    account_id = await _get_alice_id(app_client)
    mail_ids = await _get_mail_ids(app_client, account_id)

    if len(mail_ids) < 2:
        pytest.skip("Need at least 2 mails for bulk action test")

    # Clear and select 2 mails
    await app_client.post(f"/api/accounts/{account_id}/selection/clear")
    for mid in mail_ids[:2]:
        await app_client.post(
            f"/api/accounts/{account_id}/selection/toggle",
            json={"mail_id": mid},
        )

    # Bulk mark_read
    resp = await app_client.post(
        f"/api/accounts/{account_id}/selection/action",
        json={"action": "mark_read"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["action"] == "mark_read"
    assert data["affected_count"] >= 2

    # Selection should be cleared after bulk action
    sel_resp = await app_client.get(
        f"/api/accounts/{account_id}/selection",
    )
    assert sel_resp.json()["count"] == 0

    # Restore: mark them unread again
    for mid in mail_ids[:2]:
        await app_client.post(
            f"/api/mails/{mid}/action",
            params={"account_id": account_id},
            json={"action": "mark_unread"},
        )


@pytest.mark.asyncio
async def test_bulk_action_no_selection_returns_400(
    app_client: httpx.AsyncClient,
) -> None:
    """Bulk action with empty selection returns 400."""
    account_id = await _get_alice_id(app_client)

    # Ensure empty selection
    await app_client.post(f"/api/accounts/{account_id}/selection/clear")

    resp = await app_client.post(
        f"/api/accounts/{account_id}/selection/action",
        json={"action": "mark_read"},
    )
    assert resp.status_code == 400
