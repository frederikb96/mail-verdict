"""
DELETE /folders/{folder_id}'s backfill guard, against a real database.

A folder's own message count is a mirror write, not a server confirmation
-- and on a folder that has not finished its first sync yet, that count
can read zero while the server is still holding everything (PostIMAP
backfills one folder per account at a time, so on a freshly added
account most folders sit unsynced for a while). confirm_message_count
would then confirm a real folder as empty, and the delete destroys
whatever the server actually has. This is the exact shape of the
incident this guard exists for.

The pinned test PostIMAP image is well past the folder-CRUD grant's
version threshold (see test_actions_roundtrip.py's TestSupportsFolderCrud),
so no version needs seeding here to reach the guard itself.
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


async def _seed_folder(
    session: AsyncSession,
    account_id: uuid.UUID,
    imap_name: str,
    *,
    backfill_total: int | None,
    initial_sync_done: bool,
) -> uuid.UUID:
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO folders "
            "(id, account_id, imap_name, special_use, backfill_total, initial_sync_done) "
            "VALUES (:id, :account_id, :imap_name, 'archive', :backfill_total, :done)"
        ),
        {
            "id": folder_id, "account_id": account_id, "imap_name": imap_name,
            "backfill_total": backfill_total, "done": initial_sync_done,
        },
    )
    return folder_id


async def _folder_deleted_at(migrated_db: DatabaseConnection, folder_id: uuid.UUID) -> object:
    async with migrated_db.session() as session:
        return await session.scalar(select(Folder.deleted_at).where(Folder.id == folder_id))


class TestBackfillGuard:
    def test_a_folder_whose_backfill_has_not_started_is_refused(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """backfill_total IS NULL: not started at all -- the shape of the
        incident, a folder on a freshly added account nobody has reached
        yet, with the mirror reading zero messages while the server holds
        everything."""
        async def _seed(db: DatabaseConnection) -> uuid.UUID:
            async with db.session() as session:
                account_id = await _seed_account(session)
                folder_id = await _seed_folder(
                    session, account_id, "Archive",
                    backfill_total=None, initial_sync_done=False,
                )
                await session.commit()
            return folder_id

        folder_id = client.portal.call(_seed, migrated_db)
        target = "mail_verdict.api.folder_management.get_db_connection"
        with patch(target, return_value=migrated_db):
            # Even confirming the (zero) count the mirror reports must not
            # be enough -- that count is exactly what cannot be trusted.
            resp = client.delete(f"/folders/{folder_id}?confirm_message_count=0")

        assert resp.status_code == 409, resp.text
        assert "sync" in resp.json()["detail"].lower()
        assert client.portal.call(_folder_deleted_at, migrated_db, folder_id) is None

    def test_a_folder_mid_backfill_is_refused(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """backfill_total set, initial_sync_done still false: the folder
        currently being worked on -- see the contract's "Watching an
        initial sync" section."""
        async def _seed(db: DatabaseConnection) -> uuid.UUID:
            async with db.session() as session:
                account_id = await _seed_account(session)
                folder_id = await _seed_folder(
                    session, account_id, "Archive",
                    backfill_total=500, initial_sync_done=False,
                )
                await session.commit()
            return folder_id

        folder_id = client.portal.call(_seed, migrated_db)
        target = "mail_verdict.api.folder_management.get_db_connection"
        with patch(target, return_value=migrated_db):
            resp = client.delete(f"/folders/{folder_id}?confirm_message_count=0")

        assert resp.status_code == 409, resp.text
        assert "progress" in resp.json()["detail"].lower()
        assert client.portal.call(_folder_deleted_at, migrated_db, folder_id) is None

    def test_a_folder_that_finished_its_first_sync_is_not_blocked_by_this_guard(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """Control case: initial_sync_done true reaches the real delete
        rather than being rejected by this guard."""
        async def _seed(db: DatabaseConnection) -> uuid.UUID:
            async with db.session() as session:
                account_id = await _seed_account(session)
                folder_id = await _seed_folder(
                    session, account_id, "Archive",
                    backfill_total=0, initial_sync_done=True,
                )
                await session.commit()
            return folder_id

        folder_id = client.portal.call(_seed, migrated_db)
        target = "mail_verdict.api.folder_management.get_db_connection"
        with patch(target, return_value=migrated_db):
            resp = client.delete(f"/folders/{folder_id}?confirm_message_count=0")

        assert resp.status_code == 204, resp.text
        assert client.portal.call(_folder_deleted_at, migrated_db, folder_id) is not None
