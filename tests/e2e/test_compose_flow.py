"""
Compose flow through the outbox: a send is actually transmitted over SMTP
and its Sent copy syncs back into the mailbox; a draft goes to Drafts with
sent_at left null; a reply carries its threading headers onto the same
thread as the message it replies to.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Outbox
from tests.e2e.helpers import (
    unique_email,
    wait_for,
    wait_for_account_active,
    wait_for_folder,
    wait_for_mailpit_message,
)
from tests.setup.containers import (
    DOVECOT_ALIAS,
    DOVECOT_IMAP_PORT,
    DOVECOT_PASSWORD,
    MAILPIT_ALIAS,
    MAILPIT_SMTP_PORT,
)
from tests.setup.mail_delivery import build_eml, deliver_message

ORIGINAL_SUBJECT = "Original thread"


@pytest.fixture(scope="class")
def composing_account(
    app_client: TestClient, dovecot_endpoint: tuple[str, int, int],
) -> dict[str, Any]:
    """
    An active account with SMTP pointed at Mailpit and one pre-existing
    INBOX message (with a known Message-ID) to reply to.
    """
    host, _imap_port, lmtp_port = dovecot_endpoint
    email = unique_email("compose")
    original_message_id = f"<original-{uuid.uuid4()}@example.com>"

    original = build_eml(
        sender="correspondent@example.com", recipient=email, subject=ORIGINAL_SUBJECT,
        message_id=original_message_id,
    )
    deliver_message(
        original, host, lmtp_port, sender="correspondent@example.com", recipient=email,
    )

    resp = app_client.post(
        "/api/accounts",
        json={
            "name": email,
            "imap_host": DOVECOT_ALIAS,
            "imap_port": DOVECOT_IMAP_PORT,
            "imap_user": email,
            "imap_password": DOVECOT_PASSWORD,
            "smtp_host": MAILPIT_ALIAS,
            "smtp_port": MAILPIT_SMTP_PORT,
            "smtp_user": email,
            "smtp_password": "unused",  # Mailpit accepts any SMTP AUTH credentials
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_account_active(app_client, account["id"])
    account["email"] = email
    account["original_message_id"] = original_message_id
    return account


@pytest.fixture(scope="class")
def inbox_folder(app_client: TestClient, composing_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_folder(app_client, str(composing_account["id"]), "INBOX")


def _trigger_sync(app_client: TestClient, account_id: str) -> None:
    """Force an immediate sync, per the documented UX: a sent copy otherwise
    only reappears on Sent's next periodic sync (up to a minute by default)."""
    resp = app_client.post(f"/api/accounts/{account_id}/sync")
    assert resp.status_code == 200, resp.text


def _wait_for_outbox_sent(
    app_client: TestClient, account_id: str, outbox_id: str, timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Poll the outbox list until the given row's status becomes 'sent'
    (PostIMAP's meaning: "finished processing", covering both send and draft)."""
    def _check() -> dict[str, Any] | None:
        rows = app_client.get("/api/outbox", params={"account_id": account_id}).json()
        row = next((r for r in rows if r["id"] == outbox_id), None)
        return row if row is not None and row["status"] == "sent" else None

    return wait_for(
        _check, timeout_s=timeout_s,
        description=f"Outbox row {outbox_id} reaches status=sent",
    )


def _wait_for_message_in_folder(
    app_client: TestClient, account_id: str, folder_id: str, subject: str, timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Poll a folder's message list until one with the given subject appears."""
    def _check() -> dict[str, Any] | None:
        messages = app_client.get(
            f"/api/accounts/{account_id}/messages", params={"folder_id": folder_id},
        ).json()["messages"]
        return next((m for m in messages if m["subject"] == subject), None)

    return wait_for(
        _check, timeout_s=timeout_s,
        description=f"Message {subject!r} synced into folder",
    )


class TestComposeFlow:
    """Send, draft, and reply-threading, sharing one account wired to Mailpit."""

    def test_send_is_delivered_and_the_sent_copy_syncs_back(
        self, app_client: TestClient, composing_account: dict[str, Any], mailpit_http_url: str,
    ) -> None:
        subject = f"Send test {uuid.uuid4()}"
        resp = app_client.post(
            "/api/outbox",
            json={
                "account_id": composing_account["id"], "kind": "send",
                "to": ["recipient@example.com"], "subject": subject,
                "body_text": "Sent from the compose e2e test.",
            },
        )
        assert resp.status_code == 201, resp.text
        outbox_id = resp.json()["id"]

        outbox_row = _wait_for_outbox_sent(app_client, composing_account["id"], outbox_id)
        assert outbox_row["error"] is None

        delivered = wait_for_mailpit_message(mailpit_http_url, subject)
        assert delivered["To"][0]["Address"] == "recipient@example.com"

        _trigger_sync(app_client, composing_account["id"])
        sent_folder = wait_for_folder(app_client, str(composing_account["id"]), "Sent")
        _wait_for_message_in_folder(app_client, composing_account["id"], sent_folder["id"], subject)

    @pytest.mark.asyncio
    async def test_draft_goes_to_drafts_with_sent_at_left_null(
        self,
        app_client: TestClient,
        composing_account: dict[str, Any],
        db: DatabaseConnection,
    ) -> None:
        """A draft's completion is marked by status=sent (PostIMAP finished processing
        it) with sent_at staying NULL -- that NULL is how a completed draft is told
        apart from a completed send, per the outbox contract."""
        subject = f"Draft test {uuid.uuid4()}"
        resp = app_client.post(
            "/api/outbox",
            json={
                "account_id": composing_account["id"], "kind": "draft",
                "to": ["recipient@example.com"], "subject": subject,
                "body_text": "A draft that should never be transmitted.",
            },
        )
        assert resp.status_code == 201, resp.text
        outbox_id = uuid.UUID(resp.json()["id"])

        _wait_for_outbox_sent(app_client, composing_account["id"], str(outbox_id))

        async with db.session() as session:
            result = await session.execute(select(Outbox.sent_at).where(Outbox.id == outbox_id))
            assert result.scalar_one() is None

        _trigger_sync(app_client, composing_account["id"])
        drafts_folder = wait_for_folder(app_client, str(composing_account["id"]), "Drafts")
        synced = _wait_for_message_in_folder(
            app_client, composing_account["id"], drafts_folder["id"], subject,
        )
        detail = app_client.get(f"/api/messages/{synced['id']}").json()
        assert detail["is_draft"] is True

    def test_reply_lands_on_the_same_thread_as_the_original(
        self,
        app_client: TestClient,
        composing_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        original = _wait_for_message_in_folder(
            app_client, composing_account["id"], inbox_folder["id"], ORIGINAL_SUBJECT,
        )
        original_thread_id = app_client.get(f"/api/messages/{original['id']}").json()["thread_id"]

        subject = f"Re: {ORIGINAL_SUBJECT}"
        resp = app_client.post(
            "/api/outbox",
            json={
                "account_id": composing_account["id"], "kind": "send",
                "to": ["correspondent@example.com"], "subject": subject,
                "body_text": "Replying to the original message.",
                "in_reply_to": composing_account["original_message_id"],
            },
        )
        assert resp.status_code == 201, resp.text
        outbox_id = resp.json()["id"]
        _wait_for_outbox_sent(app_client, composing_account["id"], outbox_id)

        _trigger_sync(app_client, composing_account["id"])
        sent_folder = wait_for_folder(app_client, str(composing_account["id"]), "Sent")
        reply = _wait_for_message_in_folder(
            app_client, composing_account["id"], sent_folder["id"], subject,
        )

        assert reply["thread_id"] == original_thread_id


def _wait_for_message_gone_from_folder(
    app_client: TestClient, account_id: str, folder_id: str, subject: str, timeout_s: float = 60.0,
) -> None:
    """Poll a folder's message list until none with the given subject remain."""
    def _check() -> bool | None:
        messages = app_client.get(
            f"/api/accounts/{account_id}/messages", params={"folder_id": folder_id},
        ).json()["messages"]
        return True if not any(m["subject"] == subject for m in messages) else None

    wait_for(
        _check, timeout_s=timeout_s,
        description=f"Message {subject!r} removed from folder",
    )


class TestDraftEditing:
    """Editing and sending a draft in place via outbox.replaces_message_id."""

    def test_editing_a_draft_leaves_no_duplicate_behind(
        self, app_client: TestClient, composing_account: dict[str, Any],
    ) -> None:
        original_subject = f"Draft edit test {uuid.uuid4()}"
        first = app_client.post(
            "/api/outbox",
            json={
                "account_id": composing_account["id"], "kind": "draft",
                "to": ["recipient@example.com"], "subject": original_subject,
                "body_text": "First version.",
            },
        )
        assert first.status_code == 201, first.text
        _wait_for_outbox_sent(app_client, composing_account["id"], first.json()["id"])

        _trigger_sync(app_client, composing_account["id"])
        drafts_folder = wait_for_folder(app_client, str(composing_account["id"]), "Drafts")
        original_message = _wait_for_message_in_folder(
            app_client, composing_account["id"], drafts_folder["id"], original_subject,
        )
        original_detail = app_client.get(f"/api/messages/{original_message['id']}").json()
        assert original_detail["is_draft"] is True

        edited_subject = f"Draft edit test (edited) {uuid.uuid4()}"
        second = app_client.post(
            "/api/outbox",
            json={
                "account_id": composing_account["id"], "kind": "draft",
                "to": ["recipient@example.com"], "subject": edited_subject,
                "body_text": "Now finished.",
                "replaces_message_id": original_message["id"],
            },
        )
        assert second.status_code == 201, second.text
        _wait_for_outbox_sent(app_client, composing_account["id"], second.json()["id"])

        _trigger_sync(app_client, composing_account["id"])
        _wait_for_message_in_folder(
            app_client, composing_account["id"], drafts_folder["id"], edited_subject,
        )
        _wait_for_message_gone_from_folder(
            app_client, composing_account["id"], drafts_folder["id"], original_subject,
        )

    def test_sending_a_draft_leaves_no_draft_behind(
        self, app_client: TestClient, composing_account: dict[str, Any], mailpit_http_url: str,
    ) -> None:
        draft_subject = f"Draft to send {uuid.uuid4()}"
        draft_resp = app_client.post(
            "/api/outbox",
            json={
                "account_id": composing_account["id"], "kind": "draft",
                "to": ["recipient@example.com"], "subject": draft_subject,
                "body_text": "Still a draft.",
            },
        )
        assert draft_resp.status_code == 201, draft_resp.text
        _wait_for_outbox_sent(app_client, composing_account["id"], draft_resp.json()["id"])

        _trigger_sync(app_client, composing_account["id"])
        drafts_folder = wait_for_folder(app_client, str(composing_account["id"]), "Drafts")
        draft_message = _wait_for_message_in_folder(
            app_client, composing_account["id"], drafts_folder["id"], draft_subject,
        )

        sent_subject = f"Sent from a draft {uuid.uuid4()}"
        send_resp = app_client.post(
            "/api/outbox",
            json={
                "account_id": composing_account["id"], "kind": "send",
                "to": ["recipient@example.com"], "subject": sent_subject,
                "body_text": "Finished and sent.",
                "replaces_message_id": draft_message["id"],
            },
        )
        assert send_resp.status_code == 201, send_resp.text
        send_outbox_id = send_resp.json()["id"]
        outbox_row = _wait_for_outbox_sent(app_client, composing_account["id"], send_outbox_id)
        assert outbox_row["error"] is None

        wait_for_mailpit_message(mailpit_http_url, sent_subject)

        _trigger_sync(app_client, composing_account["id"])
        sent_folder = wait_for_folder(app_client, str(composing_account["id"]), "Sent")
        _wait_for_message_in_folder(
            app_client, composing_account["id"], sent_folder["id"], sent_subject,
        )
        _wait_for_message_gone_from_folder(
            app_client, composing_account["id"], drafts_folder["id"], draft_subject,
        )
