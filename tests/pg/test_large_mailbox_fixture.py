"""
tests/setup/large_mailbox.py, exercised against a real migrated schema --
this is what makes this fixture's own acceptance criterion provable: a
test can ask for at least a thousand messages and get exactly that, shaped
well enough for a mail-list test to act on.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from tests.setup.large_mailbox import (
    LARGE_MAILBOX_MIN_COUNT,
    build_large_mailbox,
    seed_large_mailbox,
    seed_large_mailbox_account,
)


async def _message_count(session: AsyncSession, folder_id: uuid.UUID) -> int:
    result = await session.execute(
        text("SELECT count(*) FROM messages WHERE folder_id = :folder_id"),
        {"folder_id": folder_id},
    )
    return int(result.scalar_one())


class TestLargeMailboxFixture:
    @pytest.mark.asyncio
    async def test_requests_at_least_a_thousand_messages(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The acceptance floor: asking for the minimum count actually
        yields that many distinct rows, each with the shape a mail-list
        test needs -- a subject, a sender, a bracketed message_id, its own
        thread, and a received_at that orders the same way imap_uid does."""
        async with migrated_db.session() as session:
            _account_id, folder_id, message_ids = await build_large_mailbox(
                session, LARGE_MAILBOX_MIN_COUNT,
            )
            await session.commit()

        assert len(message_ids) == LARGE_MAILBOX_MIN_COUNT
        assert len(set(message_ids)) == LARGE_MAILBOX_MIN_COUNT  # no accidental collisions

        async with migrated_db.session() as session:
            assert await _message_count(session, folder_id) == LARGE_MAILBOX_MIN_COUNT

            result = await session.execute(
                text(
                    "SELECT is_seen, subject, from_addr, thread_id, received_at, message_id "
                    "FROM messages WHERE folder_id = :folder_id ORDER BY imap_uid"
                ),
                {"folder_id": folder_id},
            )
            rows = result.all()

        assert len(rows) == LARGE_MAILBOX_MIN_COUNT
        assert all(r.subject and r.from_addr for r in rows)
        assert all(r.message_id.startswith("<") and r.message_id.endswith(">") for r in rows)
        # Every thread is its own, single-message thread here -- the
        # fixture is not faithful to real multi-message threading, see
        # the module docstring for why.
        assert len({r.thread_id for r in rows}) == LARGE_MAILBOX_MIN_COUNT
        # received_at rises with imap_uid, so a date-ordered scroll
        # position lands where a test using this fixture expects it to.
        received = [r.received_at for r in rows]
        assert received == sorted(received)
        # Neither an all-read nor an all-unread folder -- both extremes
        # exercise nothing about an unread count or a bulk mark-read.
        assert {r.is_seen for r in rows} == {True, False}

    @pytest.mark.asyncio
    async def test_second_batch_into_same_folder_does_not_collide(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """uid_start lets a caller top up an existing large mailbox without
        tripping UNIQUE(folder_id, imap_uid) -- the same shape
        test_bulk_actions_and_outbox.py's own _seed_messages relies on."""
        async with migrated_db.session() as session:
            account_id, folder_id = await seed_large_mailbox_account(session)
            first = await seed_large_mailbox(session, account_id, folder_id, 50)
            second = await seed_large_mailbox(
                session, account_id, folder_id, 50, uid_start=len(first) + 1,
            )
            await session.commit()

        assert set(first).isdisjoint(second)
        async with migrated_db.session() as session:
            assert await _message_count(session, folder_id) == 100
