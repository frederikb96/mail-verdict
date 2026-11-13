"""
Two users' mail on one database: what the mail routes refuse and what they
keep apart, driven through a bare app that acts as each user in turn.

The e2e isolation harness checks the same routes with real tokens against
real servers; these tests reach the cases it cannot -- two accounts that
hold the same conversation, one idempotency key sent by two users -- with
rows written directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from mail_verdict.api.mails import account_router, router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Message, MessageActionSubmission
from tests.helpers.auth import act_as, user_scope
from tests.pg.test_bulk_actions_and_outbox import _seed_account_two_folders, _seed_messages

_MAILS_TARGET = "mail_verdict.api.mails.get_db_connection"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)
    app.include_router(account_router)
    with TestClient(app) as c:
        yield c


async def _seed_user(migrated_db: DatabaseConnection) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with migrated_db.session() as session:
        await session.execute(
            text("INSERT INTO users (id, display_name) VALUES (:id, :name)"),
            {"id": user_id, "name": f"user-{user_id}"},
        )
    return user_id


async def _seed_mailbox(
    migrated_db: DatabaseConnection, thread_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """An account with one unread message: (account, inbox, junk, message)."""
    async with migrated_db.session() as session:
        account_id, inbox_id, junk_id = await _seed_account_two_folders(session)
        (message_id,) = await _seed_messages(session, account_id, inbox_id, 1)
        if thread_id is not None:
            await session.execute(
                text("UPDATE messages SET thread_id = :thread WHERE id = :id"),
                {"thread": thread_id, "id": message_id},
            )
    return account_id, inbox_id, junk_id, message_id


async def _is_seen(migrated_db: DatabaseConnection, message_id: uuid.UUID) -> bool:
    async with migrated_db.session() as session:
        return bool(
            await session.scalar(select(Message.is_seen).where(Message.id == message_id))
        )


async def _folder_of(migrated_db: DatabaseConnection, message_id: uuid.UUID) -> uuid.UUID:
    async with migrated_db.session() as session:
        folder_id = await session.scalar(select(Message.folder_id).where(Message.id == message_id))
    assert folder_id is not None
    return folder_id


class TestAThreadStaysInItsAccount:
    def test_the_same_conversation_delivered_to_two_accounts_is_two_threads(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """A mailing list message reaches two users' accounts with one
        thread id in both; one user's thread must not list the other's copy."""
        thread = uuid.uuid4()
        mine, _inbox, _junk, my_message = client.portal.call(
            _seed_mailbox, migrated_db, thread,
        )
        _theirs, _, _, their_message = client.portal.call(_seed_mailbox, migrated_db, thread)
        act_as(client.app, user_scope(uuid.uuid4(), [mine]))  # type: ignore[arg-type]

        with patch(_MAILS_TARGET, return_value=migrated_db):
            response = client.get(f"/messages/{my_message}/thread")

        assert response.status_code == 200, response.text
        assert [m["id"] for m in response.json()["messages"]] == [str(my_message)]
        assert str(their_message) not in response.text


class TestAnotherUsersMailIsOffLimits:
    def test_a_message_of_someone_elses_cannot_be_read_or_changed(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account, inbox, junk, message = client.portal.call(_seed_mailbox, migrated_db)
        stranger = user_scope(uuid.uuid4(), [uuid.uuid4()])
        act_as(client.app, stranger)  # type: ignore[arg-type]

        with patch(_MAILS_TARGET, return_value=migrated_db):
            read = client.get(f"/messages/{message}")
            acted = client.post(f"/messages/{message}/action", json={"action": "mark_read"})
            moved = client.post(
                f"/messages/{message}/action",
                json={"action": "move", "target_folder_id": str(junk)},
            )
            bulk = client.post(
                f"/accounts/{account}/messages/bulk-action",
                json={"action": "expunge", "ids": [str(message)]},
            )
            listing = client.get(f"/accounts/{account}/messages")

        assert [r.status_code for r in (read, acted, moved, bulk, listing)] == [403] * 5
        assert client.portal.call(_is_seen, migrated_db, message) is False
        assert client.portal.call(_folder_of, migrated_db, message) == inbox

    def test_a_bulk_request_cannot_name_a_message_in_another_account(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """The path account is the caller's own; an id of someone else's in
        the body resolves to nothing and acts on nothing."""
        mine, _inbox, _junk, _my_message = client.portal.call(_seed_mailbox, migrated_db)
        _theirs, _, _, their_message = client.portal.call(_seed_mailbox, migrated_db)
        act_as(client.app, user_scope(uuid.uuid4(), [mine]))  # type: ignore[arg-type]

        with patch(_MAILS_TARGET, return_value=migrated_db):
            response = client.post(
                f"/accounts/{mine}/messages/bulk-action",
                json={"action": "mark_read", "ids": [str(their_message)]},
            )

        assert response.status_code == 200, response.text
        assert response.json()["affected_count"] == 0
        assert client.portal.call(_is_seen, migrated_db, their_message) is False


class TestAnIdempotencyKeyIsEachUsersOwn:
    def test_two_users_sending_one_key_each_act_once_on_their_own_mail(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        first_user = client.portal.call(_seed_user, migrated_db)
        second_user = client.portal.call(_seed_user, migrated_db)
        first_account, _, _, first_message = client.portal.call(_seed_mailbox, migrated_db)
        second_account, _, _, second_message = client.portal.call(_seed_mailbox, migrated_db)
        key = str(uuid.uuid4())
        body = {"action": "mark_read", "idempotency_key": key}

        with patch(_MAILS_TARGET, return_value=migrated_db):
            act_as(client.app, user_scope(first_user, [first_account]))  # type: ignore[arg-type]
            first = client.post(f"/messages/{first_message}/action", json=body)
            act_as(client.app, user_scope(second_user, [second_account]))  # type: ignore[arg-type]
            second = client.post(f"/messages/{second_message}/action", json=body)

        assert first.status_code == 200 and second.status_code == 200, (first.text, second.text)
        assert first.json()["message_id"] == str(first_message)
        assert second.json()["message_id"] == str(second_message)
        assert client.portal.call(_is_seen, migrated_db, first_message) is True
        assert client.portal.call(_is_seen, migrated_db, second_message) is True
        assert client.portal.call(_submissions_by, migrated_db, [first_user, second_user]) == 2

    def test_a_user_repeating_their_own_key_is_still_answered_with_the_first_response(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        user = client.portal.call(_seed_user, migrated_db)
        account, _inbox, junk, message = client.portal.call(_seed_mailbox, migrated_db)
        body = {
            "action": "move", "target_folder_id": str(junk), "idempotency_key": str(uuid.uuid4()),
        }
        act_as(client.app, user_scope(user, [account]))  # type: ignore[arg-type]

        settings = MagicMock()
        settings.get_for_user.return_value = {"mark_read_on_file_to_archive_or_junk": True}
        with (
            patch(_MAILS_TARGET, return_value=migrated_db),
            patch("mail_verdict.api.mails.get_settings_service", return_value=settings),
        ):
            first = client.post(f"/messages/{message}/action", json=body)
            repeat = client.post(f"/messages/{message}/action", json=body)

        assert first.status_code == repeat.status_code == 200
        assert repeat.json() == first.json()
        assert client.portal.call(_submissions_by, migrated_db, [user]) == 1


async def _submissions_by(migrated_db: DatabaseConnection, users: list[uuid.UUID]) -> int:
    async with migrated_db.session() as session:
        count = await session.scalar(
            select(func.count()).select_from(MessageActionSubmission).where(
                MessageActionSubmission.user_id.in_(users),
            )
        )
    return int(count or 0)
