"""
The spam review screen's read query and its two write paths, end to end
through the real API: listing an undecided verdict, confirming one with no
move, and rejecting one the pipeline already moved to Junk -- proving it
moves back to the inbox. Verdicts are seeded directly through
VerdictRepository rather than the real classifier, which this proves
nothing about.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Message, VerdictSource
from mail_verdict.database.repository import VerdictRepository
from tests.e2e.helpers import unique_email, wait_for_account_active, wait_for_folder
from tests.setup.containers import DOVECOT_ALIAS, DOVECOT_IMAP_PORT, DOVECOT_PASSWORD
from tests.setup.mail_delivery import build_eml, deliver_message


@pytest.fixture(scope="module")
def synced_account(
    app_client: TestClient, dovecot_endpoint: tuple[str, int, int],
) -> dict[str, Any]:
    """An active account with four INBOX messages, ready for verdicts to be
    seeded directly against."""
    host, _imap_port, lmtp_port = dovecot_endpoint
    email = unique_email("spam-review")

    for i in range(4):
        message = build_eml(
            sender="sender@example.com", recipient=email, subject=f"Spam review seed {i}",
            message_id=f"<spam-review-seed-{uuid.uuid4()}@example.com>",
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


@pytest.fixture(scope="module")
def junk_folder(app_client: TestClient, synced_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_folder(app_client, str(synced_account["id"]), "Junk")


def _list_inbox(
    app_client: TestClient, account_id: str, inbox_folder: dict[str, Any],
) -> list[dict[str, Any]]:
    resp = app_client.get(
        f"/api/accounts/{account_id}/messages", params={"folder_id": inbox_folder["id"]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["messages"]


def _spam_review_items(app_client: TestClient) -> list[dict[str, Any]]:
    resp = app_client.get("/api/verdicts/spam-review")
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def _spam_review_ids(app_client: TestClient) -> set[str]:
    return {item["message_id"] for item in _spam_review_items(app_client)}


class TestSpamReview:
    """Four seeded messages, each carried through a different outcome,
    sharing one account -- reading distinct rows from the same fixture
    rather than four separate accounts."""

    @pytest.mark.asyncio
    async def test_lists_a_message_with_an_undecided_ai_verdict(
        self,
        app_client: TestClient,
        synced_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        db: DatabaseConnection,
    ) -> None:
        target = _list_inbox(app_client, synced_account["id"], inbox_folder)[0]
        message_id = uuid.UUID(target["id"])

        await VerdictRepository(db).create_verdict(
            mail_id=message_id, account_id=uuid.UUID(synced_account["id"]),
            is_spam=True, source=VerdictSource.AI,
            model_used="test-model", reasoning="looks spammy",
        )

        items = {i["message_id"]: i for i in _spam_review_items(app_client)}
        item = items[str(message_id)]
        assert item["is_junk"] is False
        assert item["model_used"] == "test-model"
        assert item["reasoning"] == "looks spammy"

    @pytest.mark.asyncio
    async def test_accepting_removes_it_with_no_move(
        self,
        app_client: TestClient,
        synced_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        db: DatabaseConnection,
    ) -> None:
        target = _list_inbox(app_client, synced_account["id"], inbox_folder)[1]
        message_id = uuid.UUID(target["id"])
        account_id = uuid.UUID(synced_account["id"])

        await VerdictRepository(db).create_verdict(
            mail_id=message_id, account_id=account_id, is_spam=True, source=VerdictSource.AI,
        )
        assert str(message_id) in _spam_review_ids(app_client)

        resp = app_client.post(
            f"/api/mails/{message_id}/feedback",
            params={"account_id": str(account_id)},
            json={"is_spam": True},
        )
        assert resp.status_code == 200, resp.text

        assert str(message_id) not in _spam_review_ids(app_client)

        async with db.session() as session:
            result = await session.execute(
                select(Message.folder_id).where(Message.id == message_id)
            )
            assert result.scalar_one() == uuid.UUID(inbox_folder["id"]), (
                "agreeing it's spam must not move the message"
            )

    @pytest.mark.asyncio
    async def test_rejecting_a_message_already_in_junk_moves_it_back(
        self,
        app_client: TestClient,
        synced_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        junk_folder: dict[str, Any],
        db: DatabaseConnection,
    ) -> None:
        target = _list_inbox(app_client, synced_account["id"], inbox_folder)[2]
        message_id = uuid.UUID(target["id"])
        account_id = uuid.UUID(synced_account["id"])

        await VerdictRepository(db).create_verdict(
            mail_id=message_id, account_id=account_id, is_spam=True, source=VerdictSource.AI,
        )

        resp = app_client.post(
            f"/api/accounts/{account_id}/messages/bulk-action",
            json={"action": "spam", "ids": [str(message_id)]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["affected_count"] == 1

        items = {i["message_id"]: i for i in _spam_review_items(app_client)}
        assert items[str(message_id)]["is_junk"] is True

        resp = app_client.post(
            f"/api/messages/{message_id}/action", json={"action": "not_spam"},
        )
        assert resp.status_code == 200, resp.text

        assert str(message_id) not in _spam_review_ids(app_client)

        async with db.session() as session:
            result = await session.execute(
                select(Message.folder_id).where(Message.id == message_id)
            )
            assert result.scalar_one() == uuid.UUID(inbox_folder["id"]), (
                "rejecting a verdict on a message the pipeline moved to Junk "
                "must move it back to the inbox"
            )

    @pytest.mark.asyncio
    async def test_a_message_already_ruled_on_never_appears(
        self,
        app_client: TestClient,
        synced_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        db: DatabaseConnection,
    ) -> None:
        """A later USER_FEEDBACK row, even one that agrees it's spam, is what
        'ruled on' means -- the message must not resurface just because its
        own is_spam still reads true."""
        target = _list_inbox(app_client, synced_account["id"], inbox_folder)[3]
        message_id = uuid.UUID(target["id"])
        account_id = uuid.UUID(synced_account["id"])

        repo = VerdictRepository(db)
        await repo.create_verdict(
            mail_id=message_id, account_id=account_id, is_spam=True, source=VerdictSource.AI,
        )
        await repo.create_verdict(
            mail_id=message_id, account_id=account_id, is_spam=True,
            source=VerdictSource.USER_FEEDBACK,
        )

        assert str(message_id) not in _spam_review_ids(app_client)
