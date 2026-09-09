"""
The retention sweep: an account with a period set for Trash or Junk gets
whatever has sat in that folder longer than its own period permanently
removed, aged from when the sweep first observed a message sitting
there -- never from the message's own date, and never resuming a clock
a message left behind by leaving the folder and coming back -- against a
real Postgres schema.

Trash and Junk share one mechanism (retention/sweep.py's own docstring
explains why), so TestTrashRetentionSweep and TestJunkRetentionSweep
below are deliberately near-identical: the point is that the same
guarantees hold for both roles, proven independently rather than
inferred from one.
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
from mail_verdict.retention.sweep import _sweep_retention_once
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


async def _seed_role_folder(
    session, account_id: uuid.UUID, *, role: str, imap_name: str,
) -> uuid.UUID:
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use) "
            "VALUES (:id, :account_id, :imap_name, :role)"
        ),
        {"id": folder_id, "account_id": account_id, "imap_name": imap_name, "role": role},
    )
    return folder_id


async def _is_expunged(db: DatabaseConnection, message_id: uuid.UUID) -> bool:
    async with db.session() as session:
        result = await session.execute(
            select(Message.expunged_at).where(Message.id == message_id)
        )
        return result.scalar_one() is not None


async def _entry(db: DatabaseConnection, message_id: uuid.UUID) -> tuple[str, datetime] | None:
    """(role, entered_at) for this message's retention_entries row, or
    None if it has none."""
    async with db.session() as session:
        result = await session.execute(
            text("SELECT role, entered_at FROM retention_entries WHERE message_id = :id"),
            {"id": message_id},
        )
        row = result.one_or_none()
        return (row.role, row.entered_at) if row is not None else None


async def _backdate_entry(db: DatabaseConnection, message_id: uuid.UUID, days: int) -> None:
    async with db.session() as session:
        await session.execute(
            text(
                "UPDATE retention_entries SET entered_at = now() - make_interval(days => :d) "
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
            trash_id = await _seed_role_folder(
                session, account_id, role="trash", imap_name="Trash",
            )
            message_id = await _seed_message(session, account_id, trash_id, received_at=None)
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(account_id, trash_retention_days=30)
        await _sweep_retention_once(migrated_db)

        entry = await _entry(migrated_db, message_id)
        assert entry is not None
        assert entry[0] == "trash"
        assert await _is_expunged(migrated_db, message_id) is False

    @pytest.mark.asyncio
    async def test_a_message_already_ancient_still_gets_the_full_window(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The message's own date must never be used -- seeded with a
        received_at a decade old, it must still survive its first sweep."""
        async with migrated_db.session() as session:
            account_id, _inbox_id, _junk_id = await _seed_account_two_folders(session)
            trash_id = await _seed_role_folder(
                session, account_id, role="trash", imap_name="Trash",
            )
            message_id = await _seed_message(
                session, account_id, trash_id,
                received_at=datetime.now(UTC) - timedelta(days=3650),
            )
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(account_id, trash_retention_days=30)
        await _sweep_retention_once(migrated_db)

        assert await _is_expunged(migrated_db, message_id) is False

    @pytest.mark.asyncio
    async def test_expunged_once_its_own_time_in_trash_expires(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, _inbox_id, _junk_id = await _seed_account_two_folders(session)
            trash_id = await _seed_role_folder(
                session, account_id, role="trash", imap_name="Trash",
            )
            message_id = await _seed_message(session, account_id, trash_id, received_at=None)
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(account_id, trash_retention_days=30)
        await _sweep_retention_once(migrated_db)
        await _backdate_entry(migrated_db, message_id, days=40)
        await _sweep_retention_once(migrated_db)

        assert await _is_expunged(migrated_db, message_id) is True
        assert await _entry(migrated_db, message_id) is None

    @pytest.mark.asyncio
    async def test_retention_unset_never_stamps_or_removes(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, _inbox_id, _junk_id = await _seed_account_two_folders(session)
            trash_id = await _seed_role_folder(
                session, account_id, role="trash", imap_name="Trash",
            )
            message_id = await _seed_message(
                session, account_id, trash_id,
                received_at=datetime.now(UTC) - timedelta(days=3650),
            )
            await session.commit()

        # No AccountPrefs row at all -- the common case for an account
        # that has never touched the setting, not just an explicit NULL.
        await _sweep_retention_once(migrated_db)

        assert await _entry(migrated_db, message_id) is None
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
        await _sweep_retention_once(migrated_db)

        assert await _entry(migrated_db, inbox_message) is None
        # junk_retention_days is unset, so the junk-role sweep leaves
        # this one alone too -- see TestJunkRetentionSweep for its own
        # positive coverage.
        assert await _entry(migrated_db, junk_message) is None

    @pytest.mark.asyncio
    async def test_leaving_trash_and_returning_resets_the_clock(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """A message old enough to be overdue is rescued out of Trash
        before the sweep ever removes it, then trashed again -- it must
        not be expunged on the strength of the clock it left behind."""
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            trash_id = await _seed_role_folder(
                session, account_id, role="trash", imap_name="Trash",
            )
            message_id = await _seed_message(session, account_id, trash_id, received_at=None)
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(account_id, trash_retention_days=30)
        await _sweep_retention_once(migrated_db)
        await _backdate_entry(migrated_db, message_id, days=40)

        # Rescued back to the inbox before the next tick ever sees it as
        # overdue -- the same race a person clicking "move to inbox" wins
        # by acting before the next 15-minute tick.
        async with migrated_db.session() as session:
            await move_message(session, message_id, inbox_id)
            await session.commit()
        await _sweep_retention_once(migrated_db)
        assert await _entry(migrated_db, message_id) is None
        assert await _is_expunged(migrated_db, message_id) is False

        # Trashed again -- a fresh clock, not the 40-day-old one.
        async with migrated_db.session() as session:
            await move_message(session, message_id, trash_id)
            await session.commit()
        await _sweep_retention_once(migrated_db)

        entry = await _entry(migrated_db, message_id)
        assert entry is not None
        assert await _is_expunged(migrated_db, message_id) is False


class TestJunkRetentionSweep:
    """Same guarantees as Trash, proven independently -- Junk is filled
    by the spam pipeline as much as by a person, and mail nobody ever
    touched by hand is exactly the case where getting the grace period
    wrong is most dangerous."""

    @pytest.mark.asyncio
    async def test_a_message_freshly_seen_in_junk_is_stamped_not_expunged(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, _inbox_id, junk_id = await _seed_account_two_folders(session)
            message_id = await _seed_message(session, account_id, junk_id, received_at=None)
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(account_id, junk_retention_days=30)
        await _sweep_retention_once(migrated_db)

        entry = await _entry(migrated_db, message_id)
        assert entry is not None
        assert entry[0] == "junk"
        assert await _is_expunged(migrated_db, message_id) is False

    @pytest.mark.asyncio
    async def test_a_message_already_ancient_still_gets_the_full_window(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, _inbox_id, junk_id = await _seed_account_two_folders(session)
            message_id = await _seed_message(
                session, account_id, junk_id,
                received_at=datetime.now(UTC) - timedelta(days=3650),
            )
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(account_id, junk_retention_days=30)
        await _sweep_retention_once(migrated_db)

        assert await _is_expunged(migrated_db, message_id) is False

    @pytest.mark.asyncio
    async def test_expunged_once_its_own_time_in_junk_expires(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, _inbox_id, junk_id = await _seed_account_two_folders(session)
            message_id = await _seed_message(session, account_id, junk_id, received_at=None)
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(account_id, junk_retention_days=30)
        await _sweep_retention_once(migrated_db)
        await _backdate_entry(migrated_db, message_id, days=40)
        await _sweep_retention_once(migrated_db)

        assert await _is_expunged(migrated_db, message_id) is True
        assert await _entry(migrated_db, message_id) is None

    @pytest.mark.asyncio
    async def test_leaving_junk_and_returning_resets_the_clock(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The exact case named for this block: rescued out of Junk,
        later re-ruled spam and filed back in -- it must get a fresh
        clock, not resume the one it left behind."""
        async with migrated_db.session() as session:
            account_id, inbox_id, junk_id = await _seed_account_two_folders(session)
            message_id = await _seed_message(session, account_id, junk_id, received_at=None)
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(account_id, junk_retention_days=30)
        await _sweep_retention_once(migrated_db)
        await _backdate_entry(migrated_db, message_id, days=40)

        # Rescued to the inbox before the next tick ever sees it as overdue.
        async with migrated_db.session() as session:
            await move_message(session, message_id, inbox_id)
            await session.commit()
        await _sweep_retention_once(migrated_db)
        assert await _entry(migrated_db, message_id) is None
        assert await _is_expunged(migrated_db, message_id) is False

        # Re-ruled spam and filed back into Junk -- a fresh clock.
        async with migrated_db.session() as session:
            await move_message(session, message_id, junk_id)
            await session.commit()
        await _sweep_retention_once(migrated_db)

        assert await _entry(migrated_db, message_id) is not None
        assert await _is_expunged(migrated_db, message_id) is False

    @pytest.mark.asyncio
    async def test_trash_and_junk_retention_age_independently(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The owner arrived at the same number for both, but by
        considering a different one for Junk first -- an account with
        different periods for the two must age each on its own."""
        async with migrated_db.session() as session:
            account_id, _inbox_id, junk_id = await _seed_account_two_folders(session)
            trash_id = await _seed_role_folder(
                session, account_id, role="trash", imap_name="Trash",
            )
            trash_message = await _seed_message(session, account_id, trash_id, received_at=None)
            junk_message = await _seed_message(session, account_id, junk_id, received_at=None)
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(
            account_id, trash_retention_days=30, junk_retention_days=7,
        )
        await _sweep_retention_once(migrated_db)
        # Both are 10 days overdue for a 7-day period, but only Trash's
        # own 30-day period leaves the trashed message untouched.
        await _backdate_entry(migrated_db, trash_message, days=10)
        await _backdate_entry(migrated_db, junk_message, days=10)
        await _sweep_retention_once(migrated_db)

        assert await _is_expunged(migrated_db, trash_message) is False
        assert await _is_expunged(migrated_db, junk_message) is True

    @pytest.mark.asyncio
    async def test_moved_directly_from_trash_to_junk_gets_a_fresh_clock(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Trash and Junk share one retention_entries row per message
        (message_id alone is the primary key -- see the model's own
        docstring on why that is safe). A message moved directly from
        one tracked role to the other, without ever passing through an
        untracked folder in between, must still end up correctly
        attributed to its new role with a fresh clock, never stuck under
        its old one and never expunged on the strength of it. Trash is
        always processed before Junk (a fixed, deterministic order --
        see _RETENTION_ROLES), so this direction resolves within the
        very next tick; see the reverse-direction test below for the
        slower, still-safe case."""
        async with migrated_db.session() as session:
            account_id, _inbox_id, junk_id = await _seed_account_two_folders(session)
            trash_id = await _seed_role_folder(
                session, account_id, role="trash", imap_name="Trash",
            )
            message_id = await _seed_message(session, account_id, trash_id, received_at=None)
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(
            account_id, trash_retention_days=30, junk_retention_days=30,
        )
        await _sweep_retention_once(migrated_db)
        await _backdate_entry(migrated_db, message_id, days=40)

        async with migrated_db.session() as session:
            await move_message(session, message_id, junk_id)
            await session.commit()
        await _sweep_retention_once(migrated_db)

        entry = await _entry(migrated_db, message_id)
        assert entry is not None
        assert entry[0] == "junk"
        assert await _is_expunged(migrated_db, message_id) is False

    @pytest.mark.asyncio
    async def test_moved_directly_from_junk_to_trash_eventually_gets_a_fresh_clock(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The slower direction: Junk is processed after Trash, so a
        message moved straight from Junk into Trash keeps its stale Junk
        row for one extra tick (that role's own stamp step sees the
        existing message_id and skips it) before Junk's own cleanup step
        drops it and the following tick claims it under Trash. Delayed,
        never lost and never expunged early -- the same fail-toward-
        later bias as everywhere else in this sweep."""
        async with migrated_db.session() as session:
            account_id, _inbox_id, junk_id = await _seed_account_two_folders(session)
            trash_id = await _seed_role_folder(
                session, account_id, role="trash", imap_name="Trash",
            )
            message_id = await _seed_message(session, account_id, junk_id, received_at=None)
            await session.commit()

        await AccountPrefsRepository(migrated_db).update(
            account_id, trash_retention_days=30, junk_retention_days=30,
        )
        await _sweep_retention_once(migrated_db)
        await _backdate_entry(migrated_db, message_id, days=40)

        async with migrated_db.session() as session:
            await move_message(session, message_id, trash_id)
            await session.commit()

        # First tick after the move: still not attributed to Trash yet.
        await _sweep_retention_once(migrated_db)
        assert await _is_expunged(migrated_db, message_id) is False

        # Second tick: Junk's cleanup has dropped the stale row, and
        # this tick's Trash stamp claims it fresh.
        await _sweep_retention_once(migrated_db)
        entry = await _entry(migrated_db, message_id)
        assert entry is not None
        assert entry[0] == "trash"
        assert await _is_expunged(migrated_db, message_id) is False
