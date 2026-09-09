"""
The Trash retention sweep: an account with account_prefs.trash_retention_days
set gets its overdue trash permanently removed on a schedule -- the one
thing the pipeline's arrival-only trigger cannot express, against a real
Postgres schema.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Message
from mail_verdict.database.repository import AccountPrefsRepository
from mail_verdict.retention.sweep import _sweep_trash_once
from tests.pg.test_bulk_actions_and_outbox import _seed_account_two_folders

_imap_uid_counter = itertools.count(1)


async def _seed_message(
    session, account_id: uuid.UUID, folder_id: uuid.UUID, *, received_at: datetime | None,
) -> uuid.UUID:
    message_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, received_at) "
            "VALUES (:id, :account_id, :folder_id, :uid, :thread_id, :msg_id, :received_at)"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": folder_id,
            "uid": next(_imap_uid_counter), "thread_id": uuid.uuid4(),
            "msg_id": f"<{message_id}@example.com>", "received_at": received_at,
        },
    )
    return message_id


async def _is_expunged(db: DatabaseConnection, message_id: uuid.UUID) -> bool:
    async with db.session() as session:
        result = await session.execute(
            select(Message.expunged_at).where(Message.id == message_id)
        )
        return result.scalar_one() is not None


class TestTrashRetentionSweep:
    @pytest.mark.asyncio
    async def test_a_message_older_than_retention_is_expunged(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, _inbox_id, junk_id = await _seed_account_two_folders(session)
            trash_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO folders (id, account_id, imap_name, special_use) "
                    "VALUES (:id, :account_id, 'Trash', 'trash')"
                ),
                {"id": trash_id, "account_id": account_id},
            )
            old_message = await _seed_message(
                session, account_id, trash_id,
                received_at=datetime.now(UTC) - timedelta(days=40),
            )
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(account_id, trash_retention_days=30)

        await _sweep_trash_once(migrated_db)

        assert await _is_expunged(migrated_db, old_message) is True

    @pytest.mark.asyncio
    async def test_a_message_newer_than_retention_is_untouched(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, _inbox_id, junk_id = await _seed_account_two_folders(session)
            trash_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO folders (id, account_id, imap_name, special_use) "
                    "VALUES (:id, :account_id, 'Trash', 'trash')"
                ),
                {"id": trash_id, "account_id": account_id},
            )
            recent_message = await _seed_message(
                session, account_id, trash_id,
                received_at=datetime.now(UTC) - timedelta(days=2),
            )
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(account_id, trash_retention_days=30)

        await _sweep_trash_once(migrated_db)

        assert await _is_expunged(migrated_db, recent_message) is False

    @pytest.mark.asyncio
    async def test_retention_unset_removes_nothing(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The default (NULL trash_retention_days) is off -- a very old
        trash message is left alone until someone sets a number."""
        async with migrated_db.session() as session:
            account_id, _inbox_id, junk_id = await _seed_account_two_folders(session)
            trash_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO folders (id, account_id, imap_name, special_use) "
                    "VALUES (:id, :account_id, 'Trash', 'trash')"
                ),
                {"id": trash_id, "account_id": account_id},
            )
            ancient_message = await _seed_message(
                session, account_id, trash_id,
                received_at=datetime.now(UTC) - timedelta(days=3650),
            )
            await session.commit()

        # No AccountPrefs row at all -- the common case for an account
        # that has never touched the setting, not just an explicit NULL.
        await _sweep_trash_once(migrated_db)

        assert await _is_expunged(migrated_db, ancient_message) is False

    @pytest.mark.asyncio
    async def test_an_old_message_outside_trash_is_untouched(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Scope is Trash only -- retention configured for the account
        must never reach into the inbox or Junk."""
        async with migrated_db.session() as session:
            account_id, inbox_id, junk_id = await _seed_account_two_folders(session)
            old_in_inbox = await _seed_message(
                session, account_id, inbox_id,
                received_at=datetime.now(UTC) - timedelta(days=400),
            )
            old_in_junk = await _seed_message(
                session, account_id, junk_id,
                received_at=datetime.now(UTC) - timedelta(days=400),
            )
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(account_id, trash_retention_days=30)

        await _sweep_trash_once(migrated_db)

        assert await _is_expunged(migrated_db, old_in_inbox) is False
        assert await _is_expunged(migrated_db, old_in_junk) is False
