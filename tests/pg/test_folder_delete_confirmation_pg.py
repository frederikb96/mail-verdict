"""
DELETE /folders/{folder_id}'s confirm_message_count requirement, against a
real database.

Folder deletion destroys every message in the folder on the mail server,
irreversibly (see the module's own docstring). The browser UI's own
confirmation dialog is not a REST-layer guarantee -- an API or MCP client
gets nothing standing between one DELETE call and permanent data loss
without this.

The pinned test PostIMAP image is well past the folder-CRUD grant's
version threshold (see test_actions_roundtrip.py's TestSupportsFolderCrud),
so no version needs seeding here to reach the confirmation check itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.folder_management import folder_prefs_router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Folder


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(folder_prefs_router)
    with TestClient(app) as c:
        yield c


async def _seed_account(session: AsyncSession) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    return account_id


async def _seed_folder(session: AsyncSession, account_id: uuid.UUID, imap_name: str) -> uuid.UUID:
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use) "
            "VALUES (:id, :account_id, :imap_name, 'archive')"
        ),
        {"id": folder_id, "account_id": account_id, "imap_name": imap_name},
    )
    return folder_id


async def _seed_messages(
    session: AsyncSession, account_id: uuid.UUID, folder_id: uuid.UUID, count: int,
) -> None:
    for i in range(count):
        message_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, account_id, folder_id, imap_uid, thread_id, message_id) "
                "VALUES (:id, :account_id, :folder_id, :uid, :thread_id, :msg_id)"
            ),
            {
                "id": message_id, "account_id": account_id, "folder_id": folder_id,
                "uid": i + 1, "thread_id": uuid.uuid4(), "msg_id": f"<{message_id}@example.com>",
            },
        )


async def _seed_folder_with_messages(
    migrated_db: DatabaseConnection, message_count: int,
) -> uuid.UUID:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        folder_id = await _seed_folder(session, account_id, "Archive")
        await _seed_messages(session, account_id, folder_id, message_count)
        await session.commit()
    return folder_id


async def _folder_deleted_at(migrated_db: DatabaseConnection, folder_id: uuid.UUID) -> object:
    async with migrated_db.session() as session:
        return await session.scalar(select(Folder.deleted_at).where(Folder.id == folder_id))


class TestDeleteConfirmation:
    def test_no_confirm_param_reports_the_count_and_deletes_nothing(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        folder_id = client.portal.call(_seed_folder_with_messages, migrated_db, 3)
        target = "mail_verdict.api.folder_management.get_db_connection"
        with patch(target, return_value=migrated_db):
            resp = client.delete(f"/folders/{folder_id}")

        assert resp.status_code == 409
        assert "3" in resp.json()["detail"]
        assert client.portal.call(_folder_deleted_at, migrated_db, folder_id) is None

    def test_wrong_confirm_count_is_rejected_and_deletes_nothing(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """A stale count (read before more mail arrived, say) must not
        silently go through as if it still matched."""
        folder_id = client.portal.call(_seed_folder_with_messages, migrated_db, 3)
        target = "mail_verdict.api.folder_management.get_db_connection"
        with patch(target, return_value=migrated_db):
            resp = client.delete(f"/folders/{folder_id}?confirm_message_count=2")

        assert resp.status_code == 409
        assert client.portal.call(_folder_deleted_at, migrated_db, folder_id) is None

    def test_correct_confirm_count_deletes_the_folder(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        folder_id = client.portal.call(_seed_folder_with_messages, migrated_db, 3)
        target = "mail_verdict.api.folder_management.get_db_connection"
        with patch(target, return_value=migrated_db):
            resp = client.delete(f"/folders/{folder_id}?confirm_message_count=3")

        assert resp.status_code == 204
        assert client.portal.call(_folder_deleted_at, migrated_db, folder_id) is not None

    def test_empty_folder_still_requires_confirm_message_count_zero(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """Zero messages does not mean zero confirmation -- the folder
        itself is still destroyed irreversibly."""
        folder_id = client.portal.call(_seed_folder_with_messages, migrated_db, 0)
        target = "mail_verdict.api.folder_management.get_db_connection"
        with patch(target, return_value=migrated_db):
            unconfirmed = client.delete(f"/folders/{folder_id}")
            assert unconfirmed.status_code == 409

            confirmed = client.delete(f"/folders/{folder_id}?confirm_message_count=0")
            assert confirmed.status_code == 204
