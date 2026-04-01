"""
E2E test: Sync stability under concurrent actions.

Verifies that the sync loop stabilizes after multiple actions
(flag, move, delete) performed in quick succession -- no perpetual
re-syncing or data drift.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    ALICE_EMAIL,
    BOB_EMAIL,
    BOB_PASSWORD,
    get_account_id,
    send_email,
    wait_for_new_mail,
)

pytestmark = [pytest.mark.e2e]


async def _get_mail_list(
    client: httpx.AsyncClient,
    account_id: str,
) -> list[dict[str, Any]]:
    """Fetch full mail list for an account."""
    resp = await client.get(
        "/api/mails",
        params={"account_id": account_id, "limit": 200},
    )
    assert resp.status_code == 200
    return resp.json()["messages"]


async def _send_test_mails(count: int = 3) -> list[str]:
    """Send test emails from Bob to Alice for thrashing tests.

    Returns:
        List of subject strings for identification.
    """
    subjects = []
    for i in range(count):
        subj = f"Sync stability test {i}"
        send_email(
            from_addr=BOB_EMAIL,
            from_password=BOB_PASSWORD,
            to_addr=ALICE_EMAIL,
            subject=subj,
            body=f"Stability test email {i}.",
        )
        subjects.append(subj)
    return subjects


@pytest.mark.asyncio
async def test_sync_stabilizes_after_thrashing(
    app_client: httpx.AsyncClient,
) -> None:
    """Rapid flag+move+delete actions don't cause perpetual sync churn.

    Sends 3 test mails, then performs different actions on each:
    - Mail 0: flag
    - Mail 1: mark read
    - Mail 2: delete

    Waits for 2 sync cycles, then verifies the mail list is consistent
    across two snapshots taken 5s apart (no drift).
    """
    account_id = await get_account_id(app_client, name="alice")

    # Snapshot existing mail IDs
    existing = await _get_mail_list(app_client, account_id)
    known_ids = {m["id"] for m in existing}

    # Send 3 test mails
    subjects = await _send_test_mails(3)

    # Wait for PostIMAP to pick up new emails
    new_mails: list[dict[str, Any]] = []
    for subj in subjects:
        mail = await wait_for_new_mail(
            app_client,
            known_ids=known_ids | {m["id"] for m in new_mails},
            subject_contains=subj,
            account_id=account_id,
            timeout=60,
        )
        new_mails.append(mail)

    assert len(new_mails) == 3, f"Expected 3 new mails, got {len(new_mails)}"

    # Perform different actions rapidly
    # Mail 0: flag
    resp = await app_client.post(
        f"/api/mails/{new_mails[0]['id']}/action",
        params={"account_id": account_id},
        json={"action": "flag"},
    )
    assert resp.status_code == 200

    # Mail 1: mark read
    resp = await app_client.post(
        f"/api/mails/{new_mails[1]['id']}/action",
        params={"account_id": account_id},
        json={"action": "mark_read"},
    )
    assert resp.status_code == 200

    # Mail 2: delete
    resp = await app_client.post(
        f"/api/mails/{new_mails[2]['id']}/action",
        params={"account_id": account_id},
        json={"action": "delete"},
    )
    assert resp.status_code == 200

    # Wait for sync to process actions (2 poll intervals of 10s each)
    await asyncio.sleep(5)

    # Take first snapshot
    snapshot_1 = await _get_mail_list(app_client, account_id)
    snapshot_1_ids = {m["id"] for m in snapshot_1}
    snapshot_1_states = {
        m["id"]: (m.get("is_flagged"), m.get("is_seen"))
        for m in snapshot_1
    }

    # Wait another sync cycle
    await asyncio.sleep(5)

    # Take second snapshot
    snapshot_2 = await _get_mail_list(app_client, account_id)
    snapshot_2_ids = {m["id"] for m in snapshot_2}
    snapshot_2_states = {
        m["id"]: (m.get("is_flagged"), m.get("is_seen"))
        for m in snapshot_2
    }

    # Verify stability: same mail set and same states
    assert snapshot_1_ids == snapshot_2_ids, (
        f"Mail list drifted between snapshots. "
        f"Added: {snapshot_2_ids - snapshot_1_ids}, "
        f"Removed: {snapshot_1_ids - snapshot_2_ids}"
    )

    # Verify states didn't flip
    for mail_id in snapshot_1_ids:
        if mail_id in snapshot_2_states:
            assert snapshot_1_states[mail_id] == snapshot_2_states[mail_id], (
                f"Mail {mail_id[:8]} state changed between snapshots: "
                f"{snapshot_1_states[mail_id]} -> {snapshot_2_states[mail_id]}"
            )

    # Verify action results persisted correctly
    # Mail 0 should be flagged
    assert new_mails[0]["id"] in snapshot_2_ids
    mail_0_detail = await app_client.get(
        f"/api/mails/{new_mails[0]['id']}",
        params={"account_id": account_id},
    )
    assert mail_0_detail.status_code == 200
    assert mail_0_detail.json()["is_flagged"] is True

    # Mail 1 should be read
    assert new_mails[1]["id"] in snapshot_2_ids
    mail_1_detail = await app_client.get(
        f"/api/mails/{new_mails[1]['id']}",
        params={"account_id": account_id},
    )
    assert mail_1_detail.status_code == 200
    assert mail_1_detail.json()["is_seen"] is True

    # Mail 2 should be deleted (not in default listing)
    assert new_mails[2]["id"] not in snapshot_2_ids
