"""
The threaded message list query against a real Postgres schema.

The fiddliest query in the API surface: DISTINCT ON to pick each thread's
latest message, joined against a per-thread count aggregate, re-ordered for
cursor pagination. Seeded with raw SQL (no real IMAP sync involved) --
this is about the SQL shape, not about PostIMAP's own sync engine.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.mails import _list_messages_threaded
from mail_verdict.database.connection import DatabaseConnection

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _seed_account_and_inbox(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a minimal account + one inbox folder, return their ids."""
    account_id = uuid.uuid4()
    inbox_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use) "
            "VALUES (:id, :account_id, 'INBOX', NULL)"
        ),
        {"id": inbox_id, "account_id": account_id},
    )
    return account_id, inbox_id


async def _seed_message(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    folder_id: uuid.UUID,
    thread_id: uuid.UUID,
    received_at: datetime,
    is_seen: bool,
    uid: int,
) -> uuid.UUID:
    """Insert one message row, returning its id."""
    message_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, "
            " received_at, is_seen) "
            "VALUES (:id, :account_id, :folder_id, :uid, :thread_id, :msg_id, "
            " :received_at, :is_seen)"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": folder_id,
            "uid": uid, "thread_id": thread_id, "msg_id": f"<{message_id}@example.com>",
            "received_at": received_at, "is_seen": is_seen,
        },
    )
    return message_id


@pytest.mark.asyncio
async def test_one_row_per_thread_with_counts(migrated_db: DatabaseConnection) -> None:
    """A thread with 3 messages (1 unread) collapses to one row with correct counts."""
    async with migrated_db.session() as session:
        account_id, inbox_id = await _seed_account_and_inbox(session)
        thread_id = uuid.uuid4()

        oldest = await _seed_message(
            session, account_id=account_id, folder_id=inbox_id, thread_id=thread_id,
            received_at=_BASE_TIME, is_seen=True, uid=1,
        )
        await _seed_message(
            session, account_id=account_id, folder_id=inbox_id, thread_id=thread_id,
            received_at=_BASE_TIME + timedelta(minutes=1), is_seen=True, uid=2,
        )
        newest = await _seed_message(
            session, account_id=account_id, folder_id=inbox_id, thread_id=thread_id,
            received_at=_BASE_TIME + timedelta(minutes=2), is_seen=False, uid=3,
        )
        await session.commit()

    async with migrated_db.session() as session:
        rows = await _list_messages_threaded(
            session, account_id, inbox_id, None, None, None, None, limit=50,
        )

    assert len(rows) == 1
    message, thread_count, unread_in_thread = rows[0]
    assert message.id == newest
    assert message.id != oldest
    assert thread_count == 3
    assert unread_in_thread == 1


@pytest.mark.asyncio
async def test_thread_order_follows_latest_message_not_thread_id(
    migrated_db: DatabaseConnection,
) -> None:
    """Threads are ordered by their own latest message's received_at, descending.

    DISTINCT ON's own ORDER BY must start with thread_id, so this proves the
    outer re-ordering step actually undoes that grouping order rather than
    leaking it through.
    """
    async with migrated_db.session() as session:
        account_id, inbox_id = await _seed_account_and_inbox(session)

        thread_a = uuid.uuid4()
        thread_b = uuid.uuid4()

        # Thread A's latest message is older than thread B's, but A's
        # thread_id would sort after B's alphabetically about half the time --
        # a leak of the DISTINCT ON order would be flaky, not deterministic.
        latest_a = await _seed_message(
            session, account_id=account_id, folder_id=inbox_id, thread_id=thread_a,
            received_at=_BASE_TIME, is_seen=True, uid=1,
        )
        latest_b = await _seed_message(
            session, account_id=account_id, folder_id=inbox_id, thread_id=thread_b,
            received_at=_BASE_TIME + timedelta(hours=1), is_seen=True, uid=2,
        )
        await session.commit()

    async with migrated_db.session() as session:
        rows = await _list_messages_threaded(
            session, account_id, inbox_id, None, None, None, None, limit=50,
        )

    assert [r[0].id for r in rows] == [latest_b, latest_a]


@pytest.mark.asyncio
async def test_thread_count_is_scoped_to_the_filtered_folder(
    migrated_db: DatabaseConnection,
) -> None:
    """A thread's count reflects only messages in the folder being listed.

    A message in a second folder for the same thread must not inflate the
    inbox's own thread_count -- threading crosses folders, but this list is
    scoped to one.
    """
    async with migrated_db.session() as session:
        account_id, inbox_id = await _seed_account_and_inbox(session)
        other_folder_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO folders (id, account_id, imap_name, special_use) "
                "VALUES (:id, :account_id, 'Sent', 'sent')"
            ),
            {"id": other_folder_id, "account_id": account_id},
        )
        thread_id = uuid.uuid4()

        await _seed_message(
            session, account_id=account_id, folder_id=inbox_id, thread_id=thread_id,
            received_at=_BASE_TIME, is_seen=True, uid=1,
        )
        await _seed_message(
            session, account_id=account_id, folder_id=other_folder_id, thread_id=thread_id,
            received_at=_BASE_TIME + timedelta(minutes=1), is_seen=True, uid=1,
        )
        await session.commit()

    async with migrated_db.session() as session:
        rows = await _list_messages_threaded(
            session, account_id, inbox_id, None, None, None, None, limit=50,
        )

    assert len(rows) == 1
    _message, thread_count, _unread = rows[0]
    assert thread_count == 1


@pytest.mark.asyncio
async def test_cursor_pagination_excludes_already_seen_threads(
    migrated_db: DatabaseConnection,
) -> None:
    """A cursor from the first page's last thread excludes that thread from the next."""
    async with migrated_db.session() as session:
        account_id, inbox_id = await _seed_account_and_inbox(session)

        thread_ids = [uuid.uuid4() for _ in range(3)]
        message_ids = []
        for i, tid in enumerate(thread_ids):
            mid = await _seed_message(
                session, account_id=account_id, folder_id=inbox_id, thread_id=tid,
                received_at=_BASE_TIME + timedelta(minutes=i), is_seen=True, uid=i + 1,
            )
            message_ids.append(mid)
        await session.commit()

    # First page: newest thread only. The helper fetches limit+1 rows so the
    # caller (list_messages) can detect has_more -- the same contract the
    # cursor-based /mails endpoint already uses.
    async with migrated_db.session() as session:
        first_page = await _list_messages_threaded(
            session, account_id, inbox_id, None, None, None, None, limit=1,
        )
    assert len(first_page) == 2  # limit+1 fetched
    cursor_message = first_page[0][0]
    assert cursor_message.id == message_ids[2]

    async with migrated_db.session() as session:
        second_page = await _list_messages_threaded(
            session, account_id, inbox_id, None, None,
            cursor_message.received_at, cursor_message.id, limit=50,
        )

    returned_ids = {r[0].id for r in second_page}
    assert cursor_message.id not in returned_ids
    assert returned_ids == {message_ids[0], message_ids[1]}
