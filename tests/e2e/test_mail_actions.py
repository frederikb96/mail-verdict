"""
Message actions against a real Dovecot mailbox: read/unread, flag, move,
trash. Database truth is asserted for every action; one full-depth case
also connects to Dovecot over IMAP to prove a flag change actually lands
on the real mail server, not just the local row.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Message
from tests.e2e.helpers import (
    unique_email,
    wait_for_account_active,
    wait_for_async,
    wait_for_folder,
)
from tests.setup.containers import DOVECOT_ALIAS, DOVECOT_IMAP_PORT, DOVECOT_PASSWORD
from tests.setup.imap_helpers import wait_for_flags
from tests.setup.mail_delivery import build_eml, deliver_message


@pytest.fixture(scope="class")
def synced_account(
    app_client: TestClient, dovecot_endpoint: tuple[str, int, int],
) -> dict[str, Any]:
    """An active account with three INBOX messages, ready for actions to be taken on them."""
    host, _imap_port, lmtp_port = dovecot_endpoint
    email = unique_email("actions")

    for i in range(3):
        message = build_eml(
            sender="sender@example.com", recipient=email, subject=f"Action test {i}",
            message_id=f"<action-test-{uuid.uuid4()}@example.com>",
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


@pytest.fixture(scope="class")
def inbox_folder(app_client: TestClient, synced_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_folder(app_client, str(synced_account["id"]), "INBOX")


@pytest.fixture(scope="class")
def junk_folder(app_client: TestClient, synced_account: dict[str, Any]) -> dict[str, Any]:
    """Dovecot ships this special-use folder on every fresh mailbox -- a plain move target."""
    return wait_for_folder(app_client, str(synced_account["id"]), "Junk")


@pytest.fixture(scope="class")
def trash_folder(app_client: TestClient, synced_account: dict[str, Any]) -> dict[str, Any]:
    """Dovecot ships this special-use folder on every fresh mailbox, with special_use='trash'
    reported natively -- the `trash` action's _resolve_special_folder finds it with no
    folder_prefs.special_use_override needed."""
    return wait_for_folder(app_client, str(synced_account["id"]), "Trash")


def _list_inbox(
    app_client: TestClient, account_id: str, inbox_folder: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fetch the current INBOX message list, ordered as the API returns it."""
    resp = app_client.get(
        f"/api/accounts/{account_id}/messages", params={"folder_id": inbox_folder["id"]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["messages"]


class TestMailActions:
    """Several message actions sharing one synced account and its three messages."""

    @pytest.mark.asyncio
    async def test_mark_read_updates_the_database(
        self,
        app_client: TestClient,
        synced_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        db: DatabaseConnection,
    ) -> None:
        """mark_read flips is_seen in the local row -- the DB truth the UI reads."""
        target = _list_inbox(app_client, synced_account["id"], inbox_folder)[0]

        resp = app_client.post(
            f"/api/messages/{target['id']}/action", json={"action": "mark_read"},
        )
        assert resp.status_code == 200, resp.text

        async with db.session() as session:
            result = await session.execute(
                select(Message.is_seen).where(Message.id == uuid.UUID(target["id"]))
            )
            assert result.scalar_one() is True

    def test_flag_reaches_the_real_imap_server(
        self,
        app_client: TestClient,
        synced_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        dovecot_endpoint: tuple[str, int, int],
    ) -> None:
        """
        The one full-depth case: flag through the API, then connect to
        Dovecot directly and prove \\Flagged actually landed there --
        the whole chain (API -> DB -> PostIMAP outbound STORE -> IMAP),
        not just the local UPDATE.
        """
        host, imap_port, _lmtp_port = dovecot_endpoint
        target = [
            m for m in _list_inbox(app_client, synced_account["id"], inbox_folder)
            if not m["is_flagged"]
        ][0]

        resp = app_client.post(f"/api/messages/{target['id']}/action", json={"action": "flag"})
        assert resp.status_code == 200, resp.text

        detail = app_client.get(f"/api/messages/{target['id']}").json()
        wait_for_flags(
            host, imap_port, synced_account["email"], DOVECOT_PASSWORD,
            "INBOX", detail["message_id"], {"\\Flagged"},
        )

    @pytest.mark.asyncio
    async def test_move_lands_in_the_target_folder_with_a_real_uid(
        self,
        app_client: TestClient,
        synced_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        junk_folder: dict[str, Any],
        db: DatabaseConnection,
    ) -> None:
        """
        move sets folder_id + imap_uid=NULL immediately (the optimistic
        write); this waits past that for PostIMAP to execute the real IMAP
        MOVE and write a real imap_uid back -- proving the round trip
        completed, not just the local UPDATE.
        """
        target = _list_inbox(app_client, synced_account["id"], inbox_folder)[-1]
        message_id = uuid.UUID(target["id"])
        target_folder_id = uuid.UUID(junk_folder["id"])

        resp = app_client.post(
            f"/api/messages/{target['id']}/action",
            json={"action": "move", "target_folder_id": junk_folder["id"]},
        )
        assert resp.status_code == 200, resp.text

        async def _check() -> bool | None:
            async with db.session() as session:
                result = await session.execute(
                    select(Message.folder_id, Message.imap_uid)
                    .where(Message.id == message_id)
                )
                folder_id, imap_uid = result.one()
            if folder_id == target_folder_id and imap_uid is not None:
                return True
            return None

        await wait_for_async(
            _check, timeout_s=30.0, description="Moved message gets a real imap_uid back",
        )

    @pytest.mark.asyncio
    async def test_trash_resolves_the_special_use_trash_folder(
        self,
        app_client: TestClient,
        synced_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        trash_folder: dict[str, Any],
        db: DatabaseConnection,
    ) -> None:
        """The trash action resolves the real special_use='trash' folder and moves the message."""
        target = _list_inbox(app_client, synced_account["id"], inbox_folder)[0]

        resp = app_client.post(f"/api/messages/{target['id']}/action", json={"action": "trash"})
        assert resp.status_code == 200, resp.text

        async with db.session() as session:
            result = await session.execute(
                select(Message.folder_id).where(Message.id == uuid.UUID(target["id"]))
            )
            assert result.scalar_one() == uuid.UUID(trash_folder["id"])
