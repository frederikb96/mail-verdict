"""
The selection-minting endpoint and the bulk-action request it feeds, end
to end through the real API and a real Dovecot mailbox -- the pg-layer
tests exercise the resolution helpers directly, this proves the FastAPI
route wires them together the way a client actually calls it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.e2e.helpers import unique_email, wait_for, wait_for_account_active, wait_for_folder
from tests.setup.containers import DOVECOT_ALIAS, DOVECOT_IMAP_PORT, DOVECOT_PASSWORD
from tests.setup.mail_delivery import build_eml, deliver_message


@pytest.fixture(scope="module")
def synced_account(
    app_client: TestClient, dovecot_endpoint: tuple[str, int, int],
) -> dict[str, Any]:
    """An active account with three INBOX messages, ready for a selection to be minted over."""
    host, _imap_port, lmtp_port = dovecot_endpoint
    email = unique_email("selection")

    for i in range(3):
        message = build_eml(
            sender="sender@example.com", recipient=email, subject=f"Selection seed {i}",
            message_id=f"<selection-seed-{uuid.uuid4()}@example.com>",
        )
        deliver_message(message, host, lmtp_port, sender="sender@example.com", recipient=email)

    resp = app_client.post(
        "/api/accounts",
        json={
            "name": email,
            "imap_host": DOVECOT_ALIAS,
            "imap_port": DOVECOT_IMAP_PORT,
            "imap_user": email,
            "imap_password": DOVECOT_PASSWORD,
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_account_active(app_client, account["id"])
    account["email"] = email
    return account


@pytest.fixture(scope="module")
def inbox_folder(app_client: TestClient, synced_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_folder(app_client, str(synced_account["id"]), "INBOX")


def _list_inbox(
    app_client: TestClient, account_id: str, inbox_folder: dict[str, Any],
) -> list[dict[str, Any]]:
    resp = app_client.get(
        f"/api/accounts/{account_id}/messages", params={"folder_id": inbox_folder["id"]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["messages"]


def _mint_selection(
    app_client: TestClient, account_id: str, folder_id: str,
) -> dict[str, Any]:
    resp = app_client.get(
        f"/api/accounts/{account_id}/messages/selection", params={"folder_id": folder_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestSelectionSnapshot:
    """GET .../messages/selection: an instant and a count from one statement."""

    def test_reports_the_folder_count_and_a_real_instant(
        self,
        app_client: TestClient,
        synced_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        existing = _list_inbox(app_client, synced_account["id"], inbox_folder)

        snapshot = _mint_selection(app_client, synced_account["id"], inbox_folder["id"])

        assert snapshot["count"] == len(existing)
        # A round-trippable ISO-8601 instant, not just any string.
        datetime.fromisoformat(snapshot["snapshot_at"])


class TestBulkActionScopeAndUnion:
    """The bulk-action route resolving a scope, an id list, or both together."""

    def test_scope_excludes_mail_delivered_after_the_snapshot(
        self,
        app_client: TestClient,
        dovecot_endpoint: tuple[str, int, int],
        synced_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """A message that arrives between minting a selection and acting on
        it must not be swept into the action -- the user agreed to a count
        that did not include it."""
        before = _list_inbox(app_client, synced_account["id"], inbox_folder)
        snapshot = _mint_selection(app_client, synced_account["id"], inbox_folder["id"])

        host, _imap_port, lmtp_port = dovecot_endpoint
        late_subject = f"Arrived after snapshot {uuid.uuid4()}"
        message = build_eml(
            sender="sender@example.com", recipient=synced_account["email"],
            subject=late_subject, message_id=f"<late-{uuid.uuid4()}@example.com>",
        )
        deliver_message(
            message, host, lmtp_port,
            sender="sender@example.com", recipient=synced_account["email"],
        )

        def _synced() -> dict[str, Any] | None:
            found = [
                m for m in _list_inbox(app_client, synced_account["id"], inbox_folder)
                if m["subject"] == late_subject
            ]
            return found[0] if found else None

        late_message = wait_for(_synced, description="the late message synced into INBOX")

        resp = app_client.post(
            f"/api/accounts/{synced_account['id']}/messages/bulk-action",
            json={
                "action": "mark_read",
                "scope": {
                    "folder_id": inbox_folder["id"],
                    "snapshot_at": snapshot["snapshot_at"],
                },
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["affected_count"] == len(before)

        late_detail = app_client.get(f"/api/messages/{late_message['id']}").json()
        assert late_detail["is_seen"] is False

    def test_ids_and_scope_together_act_on_the_union(
        self,
        app_client: TestClient,
        dovecot_endpoint: tuple[str, int, int],
        synced_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """A predicate scope plus an explicit id outside it -- everything
        the scope matches, plus one row the user ticked by hand -- flags
        both, not just whichever the client happened to list first."""
        host, _imap_port, lmtp_port = dovecot_endpoint
        subject = f"Union target {uuid.uuid4()}"
        message = build_eml(
            sender="sender@example.com", recipient=synced_account["email"],
            subject=subject, message_id=f"<union-{uuid.uuid4()}@example.com>",
        )
        deliver_message(
            message, host, lmtp_port,
            sender="sender@example.com", recipient=synced_account["email"],
        )

        def _synced() -> dict[str, Any] | None:
            found = [
                m for m in _list_inbox(app_client, synced_account["id"], inbox_folder)
                if m["subject"] == subject
            ]
            return found[0] if found else None

        outside_scope_target = wait_for(_synced, description="the union target synced")

        # A scope over a folder that does not contain the extra id -- Trash,
        # empty -- exercises the union path without the scope accidentally
        # already covering the explicit id on its own.
        trash = wait_for_folder(app_client, str(synced_account["id"]), "Trash")
        snapshot = _mint_selection(app_client, synced_account["id"], trash["id"])
        assert snapshot["count"] == 0

        resp = app_client.post(
            f"/api/accounts/{synced_account['id']}/messages/bulk-action",
            json={
                "action": "flag",
                "ids": [outside_scope_target["id"]],
                "scope": {"folder_id": trash["id"], "snapshot_at": snapshot["snapshot_at"]},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["affected_count"] == 1

        detail = app_client.get(f"/api/messages/{outside_scope_target['id']}").json()
        assert detail["is_flagged"] is True

    def test_scope_without_snapshot_at_is_rejected(
        self,
        app_client: TestClient,
        synced_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """A scope naming no snapshot_at would silently mean 'everything,
        including whatever arrives before this request runs' -- the API
        refuses it rather than defaulting."""
        resp = app_client.post(
            f"/api/accounts/{synced_account['id']}/messages/bulk-action",
            json={"action": "mark_read", "scope": {"folder_id": inbox_folder["id"]}},
        )
        assert resp.status_code == 422, resp.text
