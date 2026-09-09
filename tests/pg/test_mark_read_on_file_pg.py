"""
Mail filed into Archive or Junk -- by the toolbar action or a move that
lands there (drag-and-drop resolves to the same "move" action) -- is
marked read as it moves. settings.mail.mark_read_on_file_to_archive_or_junk
turns it off; a move anywhere else is untouched.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from mail_verdict.api.mails import account_router, router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Message
from mail_verdict.settings.service import SettingsService
from tests.pg.test_bulk_actions_and_outbox import _seed_account_two_folders, _seed_messages


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """A single persistent TestClient portal for the whole test -- a new
    `with TestClient(...)` per call would each open its own event loop in
    its own thread, and the shared migrated_db's asyncpg connections
    would then bounce between them and fail with 'attached to a
    different loop' the moment a second call touches the database."""
    app = FastAPI()
    app.include_router(router)
    app.include_router(account_router)
    with TestClient(app) as c:
        yield c


async def _settings(db: DatabaseConnection, *, mark_read: bool = True) -> SettingsService:
    """Always writes the setting explicitly -- the underlying Postgres
    database is shared across every test in this file (see
    conftest.py's migrated_db), so relying on the default merge for a
    value another test may have already written here would make this
    test's outcome depend on file order."""
    service = SettingsService(db)
    await service.load()
    await service.update("mail", {"mark_read_on_file_to_archive_or_junk": mark_read})
    return service


async def _seed_account_with_archive(
    migrated_db: DatabaseConnection,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """An account with Inbox, Junk, Archive and Trash -- returns
    (account_id, inbox_id, junk_id, archive_id, trash_id)."""
    async with migrated_db.session() as session:
        account_id, inbox_id, junk_id = await _seed_account_two_folders(session)
        archive_id = uuid.uuid4()
        trash_id = uuid.uuid4()
        for folder_id, imap_name, special_use in (
            (archive_id, "Archive", "archive"), (trash_id, "Trash", "trash"),
        ):
            await session.execute(
                text(
                    "INSERT INTO folders (id, account_id, imap_name, special_use) "
                    "VALUES (:id, :account_id, :imap_name, :special_use)"
                ),
                {
                    "id": folder_id, "account_id": account_id,
                    "imap_name": imap_name, "special_use": special_use,
                },
            )
        await session.commit()
    return account_id, inbox_id, junk_id, archive_id, trash_id


async def _seed_one_unread_message(
    migrated_db: DatabaseConnection, account_id: uuid.UUID, folder_id: uuid.UUID,
) -> uuid.UUID:
    async with migrated_db.session() as session:
        (message_id,) = await _seed_messages(session, account_id, folder_id, 1, is_seen=False)
        await session.commit()
    return message_id


async def _is_seen(migrated_db: DatabaseConnection, message_id: uuid.UUID) -> bool:
    async with migrated_db.session() as session:
        result = await session.execute(select(Message.is_seen).where(Message.id == message_id))
        return bool(result.scalar_one())


class TestSingleMessageMove:
    def test_move_into_archive_marks_it_read(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, inbox_id, _junk_id, archive_id, _trash_id = client.portal.call(
            _seed_account_with_archive, migrated_db,
        )
        message_id = client.portal.call(_seed_one_unread_message, migrated_db, account_id, inbox_id)
        settings_service = client.portal.call(_settings, migrated_db)

        with patch("mail_verdict.api.mails.get_db_connection", return_value=migrated_db), \
             patch("mail_verdict.api.mails.get_settings_service", return_value=settings_service):
            resp = client.post(
                f"/messages/{message_id}/action",
                json={"action": "move", "target_folder_id": str(archive_id)},
            )
        assert resp.status_code == 200, resp.text
        assert client.portal.call(_is_seen, migrated_db, message_id) is True

    def test_move_into_junk_marks_it_read(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, inbox_id, junk_id, _archive_id, _trash_id = client.portal.call(
            _seed_account_with_archive, migrated_db,
        )
        message_id = client.portal.call(_seed_one_unread_message, migrated_db, account_id, inbox_id)
        settings_service = client.portal.call(_settings, migrated_db)

        with patch("mail_verdict.api.mails.get_db_connection", return_value=migrated_db), \
             patch("mail_verdict.api.mails.get_settings_service", return_value=settings_service):
            resp = client.post(
                f"/messages/{message_id}/action",
                json={"action": "move", "target_folder_id": str(junk_id)},
            )
        assert resp.status_code == 200, resp.text
        assert client.portal.call(_is_seen, migrated_db, message_id) is True

    def test_the_dedicated_archive_action_marks_it_read(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, inbox_id, _junk_id, _archive_id, _trash_id = client.portal.call(
            _seed_account_with_archive, migrated_db,
        )
        message_id = client.portal.call(_seed_one_unread_message, migrated_db, account_id, inbox_id)
        settings_service = client.portal.call(_settings, migrated_db)

        with patch("mail_verdict.api.mails.get_db_connection", return_value=migrated_db), \
             patch("mail_verdict.api.mails.get_settings_service", return_value=settings_service):
            resp = client.post(f"/messages/{message_id}/action", json={"action": "archive"})
        assert resp.status_code == 200, resp.text
        assert client.portal.call(_is_seen, migrated_db, message_id) is True

    def test_move_into_an_ordinary_folder_leaves_it_unread(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """The narrow predicate: only archive/junk mark read, not every
        move -- proven against Trash, which a plain "move anywhere marks
        read" bug would also satisfy."""
        account_id, inbox_id, _junk_id, _archive_id, trash_id = client.portal.call(
            _seed_account_with_archive, migrated_db,
        )
        message_id = client.portal.call(_seed_one_unread_message, migrated_db, account_id, inbox_id)
        settings_service = client.portal.call(_settings, migrated_db)

        with patch("mail_verdict.api.mails.get_db_connection", return_value=migrated_db), \
             patch("mail_verdict.api.mails.get_settings_service", return_value=settings_service):
            resp = client.post(
                f"/messages/{message_id}/action",
                json={"action": "move", "target_folder_id": str(trash_id)},
            )
        assert resp.status_code == 200, resp.text
        assert client.portal.call(_is_seen, migrated_db, message_id) is False

    def test_the_setting_turns_it_off(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, inbox_id, _junk_id, archive_id, _trash_id = client.portal.call(
            _seed_account_with_archive, migrated_db,
        )
        message_id = client.portal.call(_seed_one_unread_message, migrated_db, account_id, inbox_id)
        settings_off = functools.partial(_settings, mark_read=False)
        settings_service = client.portal.call(settings_off, migrated_db)

        with patch("mail_verdict.api.mails.get_db_connection", return_value=migrated_db), \
             patch("mail_verdict.api.mails.get_settings_service", return_value=settings_service):
            resp = client.post(f"/messages/{message_id}/action", json={"action": "archive"})
        assert resp.status_code == 200, resp.text
        assert client.portal.call(_is_seen, migrated_db, message_id) is False


class TestBulkMove:
    def test_bulk_move_into_archive_marks_every_moved_message_read(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, inbox_id, _junk_id, _archive_id, _trash_id = client.portal.call(
            _seed_account_with_archive, migrated_db,
        )

        async def _seed_two(db: DatabaseConnection) -> list[uuid.UUID]:
            async with db.session() as session:
                ids = await _seed_messages(session, account_id, inbox_id, 2, is_seen=False)
                await session.commit()
            return ids

        ids = client.portal.call(_seed_two, migrated_db)
        settings_service = client.portal.call(_settings, migrated_db)

        with patch("mail_verdict.api.mails.get_db_connection", return_value=migrated_db), \
             patch("mail_verdict.api.mails.get_settings_service", return_value=settings_service):
            resp = client.post(
                f"/accounts/{account_id}/messages/bulk-action",
                json={"action": "archive", "ids": [str(i) for i in ids]},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["affected_count"] == 2
        for message_id in ids:
            assert client.portal.call(_is_seen, migrated_db, message_id) is True

    def test_bulk_trash_leaves_messages_unread(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, inbox_id, _junk_id, _archive_id, _trash_id = client.portal.call(
            _seed_account_with_archive, migrated_db,
        )

        async def _seed_two(db: DatabaseConnection) -> list[uuid.UUID]:
            async with db.session() as session:
                ids = await _seed_messages(session, account_id, inbox_id, 2, is_seen=False)
                await session.commit()
            return ids

        ids = client.portal.call(_seed_two, migrated_db)
        settings_service = client.portal.call(_settings, migrated_db)

        with patch("mail_verdict.api.mails.get_db_connection", return_value=migrated_db), \
             patch("mail_verdict.api.mails.get_settings_service", return_value=settings_service):
            resp = client.post(
                f"/accounts/{account_id}/messages/bulk-action",
                json={"action": "trash", "ids": [str(i) for i in ids]},
            )
        assert resp.status_code == 200, resp.text
        for message_id in ids:
            assert client.portal.call(_is_seen, migrated_db, message_id) is False
