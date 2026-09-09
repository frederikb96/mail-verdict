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
from mail_verdict.database.models import Folder, Message


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
    """A folder past its own first sync -- the state every test here except
    TestBackfillGuard cares about. See test_folder_backfill_guard_pg.py for
    the states this deliberately does not cover."""
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use, initial_sync_done) "
            "VALUES (:id, :account_id, :imap_name, 'archive', true)"
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


async def _seed_notification(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    result = await session.execute(
        text(
            "INSERT INTO sync_notifications (account_id, action, error, detail) "
            "VALUES (:account_id, 'flag_add', 'NO [CANNOT] Invalid flag', '{}'::jsonb) "
            "RETURNING id"
        ),
        {"account_id": account_id},
    )
    return int(result.scalar_one())


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


class TestWritesInFlightGuard:
    """confirm_message_count is a statement about the mirror -- it says
    nothing about a message an in-flight optimistic move has already
    reassigned to a different folder_id before the real IMAP MOVE has
    run, which the server may still be holding under the folder this
    call is about to destroy."""

    def test_a_message_moved_out_of_the_folder_blocks_its_deletion(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """The exact shape of the incident this guards against: a
        message moved OUT of the folder being deleted, still pending on
        the server, is invisible to that folder's own message_count --
        confirming the (already wrong) count must not be enough."""
        async def _seed(db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID]:
            async with db.session() as session:
                account_id = await _seed_account(session)
                folder_a = await _seed_folder(session, account_id, "Archive")
                folder_b = await _seed_folder(session, account_id, "Other")
                await _seed_messages(session, account_id, folder_a, 3)
                await session.commit()
            # An in-flight move OUT of folder_a: the row now lives under
            # folder_b with imap_uid NULL -- the same shape move_message()
            # itself produces, and the contract's own documented
            # "optimistic move pending" state. folder_a's own
            # message_count no longer mentions this row at all.
            async with db.session() as session:
                moved_id = await session.scalar(
                    select(Message.id).where(Message.folder_id == folder_a).limit(1)
                )
                await session.execute(
                    text("UPDATE messages SET folder_id = :b, imap_uid = NULL WHERE id = :id"),
                    {"b": folder_b, "id": moved_id},
                )
                await session.commit()
            return folder_a, moved_id

        folder_a, moved_id = client.portal.call(_seed, migrated_db)
        target = "mail_verdict.api.folder_management.get_db_connection"
        with patch(target, return_value=migrated_db):
            # folder_a's own message_count now reads 2 (one row already
            # reassigned to folder_b in the mirror) -- confirming that
            # already-wrong count must still be refused.
            resp = client.delete(f"/folders/{folder_a}?confirm_message_count=2")

        assert resp.status_code == 409
        detail = resp.json()["detail"].lower()
        assert "move" in detail
        # Naming the message id is what stands between this and hunting
        # for it with raw SQL against production (see this guard's own
        # docstring on the incident that cost real time doing exactly
        # that).
        assert str(moved_id) in detail
        assert client.portal.call(_folder_deleted_at, migrated_db, folder_a) is None

    def test_a_pending_move_blocks_a_different_folders_deletion_too(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """Account-wide, not folder-scoped, on purpose: an entirely
        different, empty folder on the same account is refused too --
        the pending write could resolve against any folder, and by the
        time a move's row no longer says where it came from, nothing
        short of an account-wide check can rule that out."""
        async def _seed(db: DatabaseConnection) -> uuid.UUID:
            async with db.session() as session:
                account_id = await _seed_account(session)
                folder_a = await _seed_folder(session, account_id, "Archive")
                empty_folder = await _seed_folder(session, account_id, "Empty")
                await _seed_messages(session, account_id, folder_a, 1)
                await session.commit()
            async with db.session() as session:
                message_id = await session.scalar(
                    select(Message.id).where(Message.folder_id == folder_a).limit(1)
                )
                await session.execute(
                    text("UPDATE messages SET imap_uid = NULL WHERE id = :id"),
                    {"id": message_id},
                )
                await session.commit()
            return empty_folder

        empty_folder = client.portal.call(_seed, migrated_db)
        target = "mail_verdict.api.folder_management.get_db_connection"
        with patch(target, return_value=migrated_db):
            resp = client.delete(f"/folders/{empty_folder}?confirm_message_count=0")

        assert resp.status_code == 409
        assert client.portal.call(_folder_deleted_at, migrated_db, empty_folder) is None

    def test_unacknowledged_notification_blocks_deletion_acknowledging_it_unblocks(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        async def _seed(db: DatabaseConnection) -> tuple[uuid.UUID, int]:
            async with db.session() as session:
                account_id = await _seed_account(session)
                folder_id = await _seed_folder(session, account_id, "Archive")
                notification_id = await _seed_notification(session, account_id=account_id)
                await session.commit()
            return folder_id, notification_id

        folder_id, notification_id = client.portal.call(_seed, migrated_db)
        target = "mail_verdict.api.folder_management.get_db_connection"
        with patch(target, return_value=migrated_db):
            blocked = client.delete(f"/folders/{folder_id}?confirm_message_count=0")
            assert blocked.status_code == 409
            assert "notification" in blocked.json()["detail"].lower()

            async def _ack(db: DatabaseConnection) -> None:
                async with db.session() as session:
                    await session.execute(
                        text(
                            "UPDATE sync_notifications SET acknowledged_at = now() "
                            "WHERE id = :id"
                        ),
                        {"id": notification_id},
                    )
                    await session.commit()

            client.portal.call(_ack, migrated_db)

            allowed = client.delete(f"/folders/{folder_id}?confirm_message_count=0")

        assert allowed.status_code == 204
        assert client.portal.call(_folder_deleted_at, migrated_db, folder_id) is not None
