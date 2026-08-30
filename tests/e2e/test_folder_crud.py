"""
Folder creation and deletion against a real Dovecot mailbox: a created
folder actually appears on the server and syncs back, nesting joins onto
the parent using the account's own separator, and deleting a folder
destroys the messages in it -- irreversibly, and refused outright for
INBOX rather than silently dead-lettered.
"""

from __future__ import annotations

import uuid

import pytest
from starlette.testclient import TestClient

from tests.e2e.helpers import unique_email, wait_for, wait_for_account_active, wait_for_folder
from tests.setup.containers import DOVECOT_ALIAS, DOVECOT_IMAP_PORT, DOVECOT_PASSWORD
from tests.setup.mail_delivery import build_eml, deliver_message


@pytest.fixture(scope="class")
def folder_account(
    app_client: TestClient, dovecot_endpoint: tuple[str, int, int],
) -> dict[str, object]:
    """An active account with one INBOX message, for moving into a folder under test."""
    host, _imap_port, lmtp_port = dovecot_endpoint
    email = unique_email("foldercrud")

    message = build_eml(
        sender="sender@example.com", recipient=email, subject="Move me",
        message_id=f"<{uuid.uuid4()}@example.com>",
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
    return account


class TestFolderCreation:
    """Folders created through the API actually reach the real IMAP server."""

    def test_top_level_folder_is_created_and_syncs_back(
        self, app_client: TestClient, folder_account: dict[str, object],
    ) -> None:
        resp = app_client.post(
            f"/api/accounts/{folder_account['id']}/folders", json={"name": "Archive"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["imap_name"] == "Archive"

        synced = wait_for_folder(app_client, str(folder_account["id"]), "Archive")
        assert synced["id"] == body["id"]

    def test_nested_folder_joins_onto_the_parent_with_the_separator(
        self, app_client: TestClient, folder_account: dict[str, object],
    ) -> None:
        parent_resp = app_client.post(
            f"/api/accounts/{folder_account['id']}/folders", json={"name": "Projects"},
        )
        assert parent_resp.status_code == 201, parent_resp.text
        parent = wait_for_folder(app_client, str(folder_account["id"]), "Projects")

        child_resp = app_client.post(
            f"/api/accounts/{folder_account['id']}/folders",
            json={"name": "2026", "parent_id": parent["id"]},
        )
        assert child_resp.status_code == 201, child_resp.text
        child = child_resp.json()

        # A real separator sits between the two path segments -- proves the
        # join used the account's own separator rather than concatenating.
        assert child["imap_name"] != "Projects2026"
        assert child["imap_name"].startswith("Projects")
        assert child["imap_name"].endswith("2026")

        synced_child = wait_for_folder(
            app_client, str(folder_account["id"]), child["imap_name"],
        )
        assert synced_child["id"] == child["id"]

    def test_creating_a_live_duplicate_name_is_rejected(
        self, app_client: TestClient, folder_account: dict[str, object],
    ) -> None:
        first = app_client.post(
            f"/api/accounts/{folder_account['id']}/folders", json={"name": "Duplicate"},
        )
        assert first.status_code == 201, first.text

        second = app_client.post(
            f"/api/accounts/{folder_account['id']}/folders", json={"name": "Duplicate"},
        )
        assert second.status_code == 409, second.text

    def test_creating_under_a_nonexistent_parent_404s(
        self, app_client: TestClient, folder_account: dict[str, object],
    ) -> None:
        resp = app_client.post(
            f"/api/accounts/{folder_account['id']}/folders",
            json={"name": "Orphan", "parent_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 404, resp.text


class TestFolderDeletion:
    """Deleting a folder through the API reaches the real IMAP server."""

    def test_deleting_a_folder_destroys_its_messages(
        self, app_client: TestClient, folder_account: dict[str, object],
    ) -> None:
        create_resp = app_client.post(
            f"/api/accounts/{folder_account['id']}/folders", json={"name": "ToDelete"},
        )
        assert create_resp.status_code == 201, create_resp.text
        target = wait_for_folder(app_client, str(folder_account["id"]), "ToDelete")

        inbox = wait_for_folder(app_client, str(folder_account["id"]), "INBOX")
        messages = app_client.get(
            f"/api/accounts/{folder_account['id']}/messages",
            params={"folder_id": inbox["id"]},
        ).json()["messages"]
        moved = next(m for m in messages if m["subject"] == "Move me")

        move_resp = app_client.post(
            f"/api/messages/{moved['id']}/action",
            json={"action": "move", "target_folder_id": target["id"]},
        )
        assert move_resp.status_code == 200, move_resp.text

        def _move_confirmed() -> dict[str, object] | None:
            detail = app_client.get(f"/api/messages/{moved['id']}").json()
            landed = detail["folder_id"] == target["id"] and not detail["pending_sync"]
            return detail if landed else None

        wait_for(_move_confirmed, description="Message move into ToDelete confirmed by IMAP")

        delete_resp = app_client.delete(f"/api/folders/{target['id']}")
        assert delete_resp.status_code == 204, delete_resp.text

        def _folder_emptied() -> bool | None:
            # wait_for() treats a falsy return as "not yet" -- an empty list
            # is itself falsy, so the success case has to be a bool, not the
            # (empty) list.
            messages = app_client.get(
                f"/api/accounts/{folder_account['id']}/messages",
                params={"folder_id": target["id"]},
            ).json()["messages"]
            return True if not messages else None

        wait_for(
            _folder_emptied, timeout_s=45.0,
            description="Moved message destroyed once the folder delete reaches the server",
        )

        folders = app_client.get(f"/api/accounts/{folder_account['id']}/folders").json()
        assert all(f["imap_name"] != "ToDelete" for f in folders)

    def test_deleting_inbox_is_refused(
        self, app_client: TestClient, folder_account: dict[str, object],
    ) -> None:
        inbox = wait_for_folder(app_client, str(folder_account["id"]), "INBOX")
        resp = app_client.delete(f"/api/folders/{inbox['id']}")
        assert resp.status_code == 400, resp.text

        still_there = app_client.get(f"/api/accounts/{folder_account['id']}/folders").json()
        assert any(f["imap_name"] == "INBOX" for f in still_there)

    def test_deleting_a_nonexistent_folder_404s(
        self, app_client: TestClient, folder_account: dict[str, object],
    ) -> None:
        resp = app_client.delete(f"/api/folders/{uuid.uuid4()}")
        assert resp.status_code == 404, resp.text

    def test_real_time_sync_can_be_requested_per_folder(
        self, app_client: TestClient, folder_account: dict[str, object],
    ) -> None:
        """A folder can be asked to sync in real time, and asked to stop.

        Each watched folder holds its own IMAP connection, so this is a
        budget the user spends rather than a switch they flip. What is
        asserted here is the request and the readback, which is what this
        application owns; whether the server then establishes the watch is
        PostIMAP's to answer, on its own timing, and it reports that on
        idle_status rather than through this call.
        """
        inbox = wait_for_folder(app_client, str(folder_account["id"]), "INBOX")
        assert "idle_status" in inbox

        # Establish the starting state rather than assuming it: the database
        # outlives a single test, so an earlier run is a state this one has
        # to survive.
        base = app_client.patch(
            f"/api/folders/{inbox['id']}/prefs", json={"real_time": False},
        )
        assert base.status_code == 200, base.text
        assert base.json()["idle_requested"] is False

        on = app_client.patch(f"/api/folders/{inbox['id']}/prefs", json={"real_time": True})
        assert on.status_code == 200, on.text
        assert on.json()["idle_requested"] is True

        listed = app_client.get(f"/api/accounts/{folder_account['id']}/folders").json()
        assert next(f for f in listed if f["id"] == inbox["id"])["idle_requested"] is True

        off = app_client.patch(f"/api/folders/{inbox['id']}/prefs", json={"real_time": False})
        assert off.status_code == 200, off.text
        assert off.json()["idle_requested"] is False

    def test_a_deleted_folder_disappears_from_every_listing(
        self, app_client: TestClient, folder_account: dict[str, object],
    ) -> None:
        """Deletion is a tombstone, so every listing must exclude it.

        The folder list and the ordered list the sidebar reads are separate
        queries against the same table. One filtered the tombstone and the
        other did not, so a deleted folder vanished from settings and stayed
        in the sidebar permanently -- visible, clickable, and gone from the
        mail server.
        """
        account_id = str(folder_account["id"])
        created = app_client.post(
            f"/api/accounts/{account_id}/folders", json={"name": "Ephemeral"},
        )
        assert created.status_code == 201, created.text
        folder_id = created.json()["id"]

        ordered = app_client.get(f"/api/accounts/{account_id}/folder-order").json()
        assert any(f["imap_name"] == "Ephemeral" for f in ordered["folders"])

        deleted = app_client.delete(f"/api/folders/{folder_id}")
        assert deleted.status_code == 204, deleted.text

        def _gone_everywhere() -> bool | None:
            listed = app_client.get(f"/api/accounts/{account_id}/folders").json()
            ordered_now = app_client.get(
                f"/api/accounts/{account_id}/folder-order"
            ).json()["folders"]
            in_either = any(f["imap_name"] == "Ephemeral" for f in listed) or any(
                f["imap_name"] == "Ephemeral" for f in ordered_now
            )
            return True if not in_either else None

        wait_for(
            _gone_everywhere, timeout_s=45.0,
            description="Deleted folder gone from both the list and the sidebar order",
        )
