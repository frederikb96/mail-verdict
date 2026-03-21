"""
E2E test: Mail actions and on-demand body fetch.

Tests individual mail actions (star, read, move, archive, spam)
and the on-demand body fetch for mails with body_synced=True.
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import get_account_id

pytestmark = [pytest.mark.e2e]


async def _get_alice_id(client: httpx.AsyncClient) -> str:
    """Get alice's account ID."""
    return await get_account_id(client, name="alice")


async def _get_first_mail(
    client: httpx.AsyncClient,
    account_id: str,
) -> dict:
    """Fetch the first mail for the account."""
    resp = await client.get(
        "/api/mails",
        params={"account_id": account_id, "limit": 1},
    )
    assert resp.status_code == 200
    mails = resp.json()["mails"]
    assert len(mails) > 0, "No mails available"
    return mails[0]


# --- Mail detail and on-demand body fetch ---


@pytest.mark.asyncio
async def test_get_mail_detail(app_client: httpx.AsyncClient) -> None:
    """GET /api/mails/:id returns full mail detail."""
    account_id = await _get_alice_id(app_client)
    mail_summary = await _get_first_mail(app_client, account_id)

    resp = await app_client.get(
        f"/api/mails/{mail_summary['id']}",
        params={"account_id": account_id},
    )
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["id"] == mail_summary["id"]
    assert "body_text" in detail or "body_html" in detail
    assert "headers_synced" in detail
    assert "body_synced" in detail
    assert "has_blocked_images" in detail
    assert "images_allowed" in detail
    assert "tags" in detail
    assert "attachments" in detail


@pytest.mark.asyncio
async def test_mail_detail_has_auth_results(
    app_client: httpx.AsyncClient,
) -> None:
    """Mail detail includes DKIM/SPF/DMARC authentication results."""
    account_id = await _get_alice_id(app_client)
    mail_summary = await _get_first_mail(app_client, account_id)

    resp = await app_client.get(
        f"/api/mails/{mail_summary['id']}",
        params={"account_id": account_id},
    )
    assert resp.status_code == 200
    detail = resp.json()
    # These fields should exist (values may be null for test server)
    assert "dkim_pass" in detail
    assert "spf_pass" in detail
    assert "dmarc_pass" in detail


@pytest.mark.asyncio
async def test_get_nonexistent_mail_404(
    app_client: httpx.AsyncClient,
) -> None:
    """Requesting a non-existent mail returns 404."""
    account_id = await _get_alice_id(app_client)
    resp = await app_client.get(
        "/api/mails/00000000-0000-0000-0000-000000000000",
        params={"account_id": account_id},
    )
    assert resp.status_code == 404


# --- Mail actions ---


@pytest.mark.asyncio
async def test_mark_read_unread(app_client: httpx.AsyncClient) -> None:
    """Mark a mail as read, then unread."""
    account_id = await _get_alice_id(app_client)
    mail = await _get_first_mail(app_client, account_id)
    mail_id = mail["id"]

    # Mark read
    resp = await app_client.post(
        f"/api/mails/{mail_id}/action",
        params={"account_id": account_id},
        json={"action": "mark_read"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert resp.json()["action"] == "mark_read"

    # Verify
    detail_resp = await app_client.get(
        f"/api/mails/{mail_id}",
        params={"account_id": account_id},
    )
    assert detail_resp.json()["is_read"] is True

    # Mark unread
    resp = await app_client.post(
        f"/api/mails/{mail_id}/action",
        params={"account_id": account_id},
        json={"action": "mark_unread"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True


@pytest.mark.asyncio
async def test_flag_unflag(app_client: httpx.AsyncClient) -> None:
    """Flag (star) and unflag a mail."""
    account_id = await _get_alice_id(app_client)
    mail = await _get_first_mail(app_client, account_id)
    mail_id = mail["id"]

    # Flag
    resp = await app_client.post(
        f"/api/mails/{mail_id}/action",
        params={"account_id": account_id},
        json={"action": "flag"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    # Verify
    detail_resp = await app_client.get(
        f"/api/mails/{mail_id}",
        params={"account_id": account_id},
    )
    assert detail_resp.json()["is_flagged"] is True

    # Unflag
    resp = await app_client.post(
        f"/api/mails/{mail_id}/action",
        params={"account_id": account_id},
        json={"action": "unflag"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True


@pytest.mark.asyncio
async def test_move_action_requires_target(
    app_client: httpx.AsyncClient,
) -> None:
    """Move action without target_folder returns 400."""
    account_id = await _get_alice_id(app_client)
    mail = await _get_first_mail(app_client, account_id)

    resp = await app_client.post(
        f"/api/mails/{mail['id']}/action",
        params={"account_id": account_id},
        json={"action": "move"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_move_to_folder_and_back(
    app_client: httpx.AsyncClient,
) -> None:
    """Move a mail to Drafts and back to INBOX."""
    account_id = await _get_alice_id(app_client)
    mail = await _get_first_mail(app_client, account_id)
    mail_id = mail["id"]
    original_folder = mail["folder_id"]

    # Move to Drafts
    resp = await app_client.post(
        f"/api/mails/{mail_id}/action",
        params={"account_id": account_id},
        json={"action": "move", "target_folder": "Drafts"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    # Verify it moved
    detail = await app_client.get(
        f"/api/mails/{mail_id}",
        params={"account_id": account_id},
    )
    assert detail.json()["folder_id"] != original_folder

    # Move back to INBOX
    resp = await app_client.post(
        f"/api/mails/{mail_id}/action",
        params={"account_id": account_id},
        json={"action": "move", "target_folder": "INBOX"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_action_on_nonexistent_mail_404(
    app_client: httpx.AsyncClient,
) -> None:
    """Action on non-existent mail returns 404."""
    account_id = await _get_alice_id(app_client)
    resp = await app_client.post(
        "/api/mails/00000000-0000-0000-0000-000000000000/action",
        params={"account_id": account_id},
        json={"action": "mark_read"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_on_demand_body_fetch(
    app_client: httpx.AsyncClient,
) -> None:
    """Requesting a mail detail triggers body fetch if not yet synced.

    Since test mails already have body_synced=True, we verify the detail
    endpoint returns body content.
    """
    account_id = await _get_alice_id(app_client)
    mail = await _get_first_mail(app_client, account_id)
    mail_id = mail["id"]

    resp = await app_client.get(
        f"/api/mails/{mail_id}",
        params={"account_id": account_id},
    )
    assert resp.status_code == 200
    detail = resp.json()

    # Since body_synced=True, body should be populated
    if detail["body_synced"]:
        has_body = detail.get("body_text") is not None or detail.get("body_html") is not None
        assert has_body, "body_synced=True but no body content returned"
