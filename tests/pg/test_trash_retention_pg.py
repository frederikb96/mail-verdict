"""
The Trash retention sweep: an account with account_prefs.trash_retention_days
set gets its overdue trash permanently removed, aged from when the sweep
first observed a message sitting in Trash -- never from the message's own
date, and never resuming a clock a message left behind by leaving Trash
and coming back -- against a real Postgres schema.
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
from mail_verdict.postimap.actions import move_message
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


async def _seed_trash_folder(session, account_id: uuid.UUID) -> uuid.UUID:
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use) "
            "VALUES (:id, :account_id, 'Trash', 'trash')"
        ),
        {"id": folder_id, "account_id": account_id},
    )
    return folder_id


async def _is_expunged(db: DatabaseConnection, message_id: uuid.UUID) -> bool:
    async with db.session() as session:
        result = await session.execute(
            select(Message.expunged_at).where(Message.id == message_id)
        )
        return result.scalar_one() is not None


async def _entered_trash_at(db: DatabaseConnection, message_id: uuid.UUID) -> datetime | None:
    async with db.session() as session:
        result = await session.execute(
            text("SELECT entered_trash_at FROM trash_entries WHERE message_id = :id"),
            {"id": message_id},
        )
        row = result.one_or_none()
        return row[0] if row is not None else None


async def _backdate_entry(db: DatabaseConnection, message_id: uuid.UUID, days: int) -> None:
    async with db.session() as session:
        await session.execute(
            text(
                "UPDATE trash_entries SET entered_trash_at = now() - make_interval(days => :d) "
                "WHERE message_id = :id"
            ),
            {"id": message_id, "d": days},
        )
        await session.commit()


class TestTrashRetentionSweep:
    @pytest.mark.asyncio
    async def test_a_message_freshly_seen_in_trash_is_stamped_not_expunged(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Arriving in Trash the moment retention is being checked gets a
        fresh clock, not immediate removal -- the grace period a "time in
        Trash" retention exists to give."""
        async with migrated_db.session() as session:
            account_id, _inbox_id, _junk_id = await _seed_account_two_folders(session)
            trash_id = await _seed_trash_folder(session, account_id)
            message_id = await _seed_message(session, account_id, trash_id, received_at=None)
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(account_id, trash_retention_days=30)
        await _sweep_trash_once(migrated_db)

        assert await _entered_trash_at(migrated_db, message_id) is not None
        assert await _is_expunged(migrated_db, message_id) is False

    @pytest.mark.asyncio
    async def test_a_message_already_ancient_still_gets_the_full_window(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The message's own date must never be used -- seeded with a
        received_at a decade old, it must still survive its first sweep."""
        async with migrated_db.session() as session:
            account_id, _inbox_id, _junk_id = await _seed_account_two_folders(session)
            trash_id = await _seed_trash_folder(session, account_id)
            message_id = await _seed_message(
                session, account_id, trash_id,
                received_at=datetime.now(UTC) - timedelta(days=3650),
            )
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(account_id, trash_retention_days=30)
        await _sweep_trash_once(migrated_db)

        assert await _is_expunged(migrated_db, message_id) is False

    @pytest.mark.asyncio
    async def test_expunged_once_its_own_time_in_trash_expires(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, _inbox_id, _junk_id = await _seed_account_two_folders(session)
            trash_id = await _seed_trash_folder(session, account_id)
            message_id = await _seed_message(session, account_id, trash_id, received_at=None)
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(account_id, trash_retention_days=30)
        await _sweep_trash_once(migrated_db)
        await _backdate_entry(migrated_db, message_id, days=40)
        await _sweep_trash_once(migrated_db)

        assert await _is_expunged(migrated_db, message_id) is True
        assert await _entered_trash_at(migrated_db, message_id) is None

    @pytest.mark.asyncio
    async def test_retention_unset_never_stamps_or_removes(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, _inbox_id, _junk_id = await _seed_account_two_folders(session)
            trash_id = await _seed_trash_folder(session, account_id)
            message_id = await _seed_message(
                session, account_id, trash_id,
                received_at=datetime.now(UTC) - timedelta(days=3650),
            )
            await session.commit()

        # No AccountPrefs row at all -- the common case for an account
        # that has never touched the setting, not just an explicit NULL.
        await _sweep_trash_once(migrated_db)

        assert await _entered_trash_at(migrated_db, message_id) is None
        assert await _is_expunged(migrated_db, message_id) is False

    @pytest.mark.asyncio
    async def test_a_message_outside_trash_is_never_stamped(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, junk_id = await _seed_account_two_folders(session)
            inbox_message = await _seed_message(session, account_id, inbox_id, received_at=None)
            junk_message = await _seed_message(session, account_id, junk_id, received_at=None)
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(account_id, trash_retention_days=30)
        await _sweep_trash_once(migrated_db)

        assert await _entered_trash_at(migrated_db, inbox_message) is None
        assert await _entered_trash_at(migrated_db, junk_message) is None

    @pytest.mark.asyncio
    async def test_leaving_trash_and_returning_resets_the_clock(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """A message old enough to be overdue is rescued out of Trash
        before the sweep ever removes it, then trashed again -- it must
        not be expunged on the strength of the clock it left behind."""
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            trash_id = await _seed_trash_folder(session, account_id)
            message_id = await _seed_message(session, account_id, trash_id, received_at=None)
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(account_id, trash_retention_days=30)
        await _sweep_trash_once(migrated_db)
        await _backdate_entry(migrated_db, message_id, days=40)

        # Rescued back to the inbox before the next tick ever sees it as
        # overdue -- the same race a person clicking "move to inbox" wins
        # by acting before the next 15-minute tick.
        async with migrated_db.session() as session:
            await move_message(session, message_id, inbox_id)
            await session.commit()
        await _sweep_trash_once(migrated_db)
        assert await _entered_trash_at(migrated_db, message_id) is None
        assert await _is_expunged(migrated_db, message_id) is False

        # Trashed again -- a fresh clock, not the 40-day-old one.
        async with migrated_db.session() as session:
            await move_message(session, message_id, trash_id)
            await session.commit()
        await _sweep_trash_once(migrated_db)

        assert await _entered_trash_at(migrated_db, message_id) is not None
        assert await _is_expunged(migrated_db, message_id) is False
