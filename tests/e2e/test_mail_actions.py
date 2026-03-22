"""
E2E test: Mail actions, IMAP propagation, and on-demand body fetch.

Tests individual mail actions (star, read, move, archive, spam, delete)
with verification that changes propagate to the IMAP server,
and the on-demand body fetch for mails with body_synced=True.
"""

from __future__ import annotations

import asyncio
import imaplib
import re

import httpx
import pytest

from tests.e2e.conftest import (
    ALICE_EMAIL,
    ALICE_PASSWORD,
    IMAP_HOST,
    IMAP_PORT,
    _imap_quote,
    get_account_id,
)

pytestmark = [pytest.mark.e2e]


async def _poll_imap_flags(
    uid: int,
    expected: str,
    *,
    present: bool = True,
    folder: str = "INBOX",
    timeout: float = 10.0,
    interval: float = 0.5,
) -> set[str]:
    """Poll IMAP flags until expected flag is present/absent or timeout.

    Args:
        uid: Message UID to check
        expected: Flag string (e.g. '\\\\Flagged')
        present: True to wait for flag to appear, False for removal
        folder: IMAP folder to check
        timeout: Max seconds to wait
        interval: Seconds between polls

    Returns:
        Final set of flags
    """
    deadline = asyncio.get_event_loop().time() + timeout
    flags: set[str] = set()
    while asyncio.get_event_loop().time() < deadline:
        flags = _imap_get_flags_by_uid(uid, folder=folder)
        if present and expected in flags:
            return flags
        if not present and expected not in flags:
            return flags
        await asyncio.sleep(interval)
    return flags


async def _poll_imap_uid_absent(
    uid: int,
    *,
    folder: str = "INBOX",
    timeout: float = 10.0,
    interval: float = 0.5,
) -> list[int]:
    """Poll until a UID is no longer present in an IMAP folder.

    Args:
        uid: UID that should disappear
        folder: IMAP folder to check
        timeout: Max seconds to wait
        interval: Seconds between polls

    Returns:
        Final list of UIDs in folder
    """
    deadline = asyncio.get_event_loop().time() + timeout
    uids: list[int] = []
    while asyncio.get_event_loop().time() < deadline:
        uids = _imap_list_uids(folder)
        if uid not in uids:
            return uids
        await asyncio.sleep(interval)
    return uids


async def _poll_imap_folder_nonempty(
    folder: str = "INBOX",
    *,
    timeout: float = 10.0,
    interval: float = 0.5,
) -> list[int]:
    """Poll until an IMAP folder has at least one message.

    Args:
        folder: IMAP folder to check
        timeout: Max seconds to wait
        interval: Seconds between polls

    Returns:
        List of UIDs in folder
    """
    deadline = asyncio.get_event_loop().time() + timeout
    uids: list[int] = []
    while asyncio.get_event_loop().time() < deadline:
        uids = _imap_list_uids(folder)
        if len(uids) > 0:
            return uids
        await asyncio.sleep(interval)
    return uids


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


# --- IMAP propagation tests ---


def _imap_get_flags_by_uid(
    uid: int,
    folder: str = "INBOX",
    user: str = ALICE_EMAIL,
    password: str = ALICE_PASSWORD,
) -> set[str]:
    """Fetch IMAP flags for a message by UID."""
    conn = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
    try:
        conn.login(user, password)
        conn.select(_imap_quote(folder))
        _, data = conn.uid("FETCH", str(uid), "(FLAGS)")
        if not data or not data[0]:
            return set()
        line = data[0].decode() if isinstance(data[0], bytes) else str(data[0])
        match = re.search(r"FLAGS \(([^)]*)\)", line)
        if match:
            return set(match.group(1).split())
        return set()
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _imap_list_uids(
    folder: str = "INBOX",
    user: str = ALICE_EMAIL,
    password: str = ALICE_PASSWORD,
) -> list[int]:
    """List all UIDs in an IMAP folder."""
    conn = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
    try:
        conn.login(user, password)
        conn.select(_imap_quote(folder))
        _, data = conn.search(None, "ALL")
        if not data or not data[0]:
            return []
        uids: list[int] = []
        for num in data[0].split():
            _, uid_data = conn.fetch(num, "(UID)")
            if uid_data and uid_data[0]:
                line = uid_data[0].decode() if isinstance(uid_data[0], bytes) else str(uid_data[0])
                match = re.search(r"UID (\d+)", line)
                if match:
                    uids.append(int(match.group(1)))
        return uids
    finally:
        try:
            conn.logout()
        except Exception:
            pass


async def _get_mail_with_uid(
    client: httpx.AsyncClient,
    account_id: str,
    *,
    skip: int = 0,
) -> dict:
    """Fetch a mail that has a valid UID for IMAP operations.

    Args:
        client: HTTP client
        account_id: Account UUID
        skip: Number of valid-UID mails to skip (avoids collisions with other tests)
    """
    resp = await client.get(
        "/api/mails",
        params={"account_id": account_id, "limit": 50},
    )
    assert resp.status_code == 200
    mails = resp.json()["mails"]
    found = 0
    for mail in mails:
        detail = await client.get(
            f"/api/mails/{mail['id']}",
            params={"account_id": account_id},
        )
        if detail.status_code == 200:
            d = detail.json()
            if d.get("uid") and d.get("uid") > 0:
                if found >= skip:
                    return d
                found += 1
    assert False, "No mail with valid UID found"


@pytest.mark.asyncio
async def test_flag_propagates_to_imap(app_client: httpx.AsyncClient) -> None:
    """Flag action propagates \\Flagged to IMAP server."""
    account_id = await _get_alice_id(app_client)
    mail = await _get_mail_with_uid(app_client, account_id, skip=2)
    mail_id = mail["id"]
    uid = mail["uid"]

    # Flag via API
    resp = await app_client.post(
        f"/api/mails/{mail_id}/action",
        params={"account_id": account_id},
        json={"action": "flag"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    # Poll IMAP until flag appears
    flags = await _poll_imap_flags(uid, "\\Flagged", present=True)
    assert "\\Flagged" in flags, f"Expected \\Flagged in IMAP flags, got: {flags}"

    # Unflag and verify removal
    resp = await app_client.post(
        f"/api/mails/{mail_id}/action",
        params={"account_id": account_id},
        json={"action": "unflag"},
    )
    assert resp.status_code == 200

    flags = await _poll_imap_flags(uid, "\\Flagged", present=False)
    assert "\\Flagged" not in flags, f"\\Flagged should be removed, got: {flags}"


@pytest.mark.asyncio
async def test_spam_action_moves_to_junk(app_client: httpx.AsyncClient) -> None:
    """Spam action moves mail to Junk Mail folder via IMAP.

    Requires the folder_mapping to have a "spam" key pointing to the Junk folder.
    The auto-detected mapping uses "junk" as the key, so we set it explicitly.
    """
    account_id = await _get_alice_id(app_client)

    # Get current folder mapping and add "spam" key
    mapping_resp = await app_client.get(
        f"/api/accounts/{account_id}/folder-mapping",
    )
    assert mapping_resp.status_code == 200
    mapping = mapping_resp.json()
    original_mapping = dict(mapping)

    # Set spam mapping (action looks for "spam" key, auto-detect uses "junk")
    mapping["spam"] = mapping.get("junk", "Junk Mail")
    await app_client.put(
        f"/api/accounts/{account_id}/folder-mapping",
        json=mapping,
    )

    try:
        mail = await _get_mail_with_uid(app_client, account_id, skip=3)
        mail_id = mail["id"]

        resp = await app_client.post(
            f"/api/mails/{mail_id}/action",
            params={"account_id": account_id},
            json={"action": "spam"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # Verify in API that mail moved
        detail = await app_client.get(
            f"/api/mails/{mail_id}",
            params={"account_id": account_id},
        )
        assert detail.status_code == 200
        moved_folder = detail.json()["folder_id"]
        assert moved_folder != mail["folder_id"], (
            "Mail should be in a different folder after spam"
        )

        # Poll IMAP until mail appears in Junk Mail
        junk_uids = await _poll_imap_folder_nonempty("Junk Mail")
        assert len(junk_uids) > 0, (
            "Junk Mail folder should have messages after spam action"
        )

        # Move back to INBOX for test cleanup
        resp = await app_client.post(
            f"/api/mails/{mail_id}/action",
            params={"account_id": account_id},
            json={"action": "move", "target_folder": "INBOX"},
        )
        assert resp.status_code == 200
        # Brief wait for cleanup move to propagate
        await asyncio.sleep(2)
    finally:
        # Restore original folder mapping
        await app_client.put(
            f"/api/accounts/{account_id}/folder-mapping",
            json=original_mapping,
        )


@pytest.mark.asyncio
async def test_delete_action_sets_deleted_flag(app_client: httpx.AsyncClient) -> None:
    """Delete action sets is_deleted in the API and propagates \\Deleted to IMAP.

    After IMAP propagation, Stalwart auto-expunges deleted messages,
    so we verify the API-level is_deleted flag and that the mail
    no longer appears in the default (non-deleted) mail listing.
    """
    account_id = await _get_alice_id(app_client)
    mail = await _get_mail_with_uid(app_client, account_id, skip=4)
    mail_id = mail["id"]
    uid = mail["uid"]

    # Get mails before delete
    before_resp = await app_client.get(
        "/api/mails",
        params={"account_id": account_id, "limit": 200},
    )
    before_ids = {m["id"] for m in before_resp.json()["mails"]}
    assert mail_id in before_ids

    resp = await app_client.post(
        f"/api/mails/{mail_id}/action",
        params={"account_id": account_id},
        json={"action": "delete"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    # Deleted mail should be excluded from default listing (is_deleted filter)
    after_resp = await app_client.get(
        "/api/mails",
        params={"account_id": account_id, "limit": 200},
    )
    after_ids = {m["id"] for m in after_resp.json()["mails"]}
    assert mail_id not in after_ids, "Deleted mail still in default listing"

    # Poll IMAP until UID is expunged from INBOX
    inbox_uids = await _poll_imap_uid_absent(uid)
    assert uid not in inbox_uids, (
        f"UID {uid} should be expunged from INBOX after delete"
    )


@pytest.mark.asyncio
async def test_archive_requires_mapped_folder(
    app_client: httpx.AsyncClient,
) -> None:
    """Archive action returns 400 when no archive folder is mapped."""
    account_id = await _get_alice_id(app_client)
    mail = await _get_first_mail(app_client, account_id)
    mail_id = mail["id"]

    resp = await app_client.post(
        f"/api/mails/{mail_id}/action",
        params={"account_id": account_id},
        json={"action": "archive"},
    )
    # Archive folder is not mapped in test env (null in folder_mapping)
    assert resp.status_code == 400
    assert "archive" in resp.json()["detail"].lower()
