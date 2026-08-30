"""
Cursor pagination when a message has no received_at, or when several
share one -- against a real Postgres schema, since the bug this guards
is in how the WHERE clause interacts with PostgreSQL's NULL ordering.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.mails import list_messages
from mail_verdict.api.unified import list_unified_messages
from mail_verdict.database.connection import DatabaseConnection

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _seed_account_and_inbox(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
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
    received_at: datetime | None,
    uid: int,
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
            "uid": uid, "thread_id": message_id, "msg_id": f"<{message_id}@example.com>",
            "received_at": received_at,
        },
    )
    return message_id


async def _paginate_all(account_id: uuid.UUID) -> list[uuid.UUID]:
    """Walk every page of GET /accounts/{id}/messages with limit=1, as a
    real client would -- following next_cursor until has_more is false."""
    seen: list[uuid.UUID] = []
    cursor: uuid.UUID | None = None
    for _ in range(20):  # hard cap: a bug here can otherwise loop forever
        page = await list_messages(
            account_id=account_id, folder_id=None, threaded=False,
            is_seen=None, since=None, before=cursor, limit=1,
        )
        seen.extend(uuid.UUID(m.id) if isinstance(m.id, str) else m.id for m in page.messages)
        if not page.has_more:
            break
        assert page.next_cursor is not None
        cursor = uuid.UUID(page.next_cursor)
    return seen


@pytest.mark.asyncio
async def test_pagination_visits_every_message_once_with_a_null_received_at(
    migrated_db: DatabaseConnection,
) -> None:
    """A message with no received_at is neither repeated nor dropped as
    pagination walks past it -- the exact failure a naive cursor predicate
    produces (see core/cursor.py)."""
    async with migrated_db.session() as session:
        account_id, inbox_id = await _seed_account_and_inbox(session)
        expected_ids = set()
        expected_ids.add(
            await _seed_message(
                session, account_id=account_id, folder_id=inbox_id,
                received_at=None, uid=1,
            )
        )
        for i in range(4):
            expected_ids.add(
                await _seed_message(
                    session, account_id=account_id, folder_id=inbox_id,
                    received_at=_BASE_TIME + timedelta(hours=i), uid=2 + i,
                )
            )

    seen = await _paginate_all(account_id)

    assert len(seen) == len(expected_ids), "pagination repeated or dropped a page"
    assert set(seen) == expected_ids


@pytest.mark.asyncio
async def test_pagination_stable_with_two_null_received_at(
    migrated_db: DatabaseConnection,
) -> None:
    """More than one NULL-received_at row in a row: the cursor still
    advances past the whole group instead of resetting or stalling."""
    async with migrated_db.session() as session:
        account_id, inbox_id = await _seed_account_and_inbox(session)
        expected_ids = {
            await _seed_message(
                session, account_id=account_id, folder_id=inbox_id, received_at=None, uid=1,
            ),
            await _seed_message(
                session, account_id=account_id, folder_id=inbox_id, received_at=None, uid=2,
            ),
            await _seed_message(
                session, account_id=account_id, folder_id=inbox_id,
                received_at=_BASE_TIME, uid=3,
            ),
        }

    seen = await _paginate_all(account_id)

    assert len(seen) == len(expected_ids)
    assert set(seen) == expected_ids


@pytest.mark.asyncio
async def test_unified_pagination_stable_with_a_null_received_at(
    migrated_db: DatabaseConnection,
) -> None:
    """The same cursor logic, exercised through the unified (cross-account)
    list endpoint, which builds its own predicate independently."""
    async with migrated_db.session() as session:
        account_id, inbox_id = await _seed_account_and_inbox(session)
        await session.execute(
            text(
                "UPDATE accounts SET is_active = true WHERE id = :id"
            ),
            {"id": account_id},
        )
        await session.execute(
            text(
                "INSERT INTO folder_prefs (folder_id, unified_name) "
                "VALUES (:folder_id, 'Inbox')"
            ),
            {"folder_id": inbox_id},
        )
        expected_ids = set()
        expected_ids.add(
            await _seed_message(
                session, account_id=account_id, folder_id=inbox_id, received_at=None, uid=1,
            )
        )
        for i in range(3):
            expected_ids.add(
                await _seed_message(
                    session, account_id=account_id, folder_id=inbox_id,
                    received_at=_BASE_TIME + timedelta(hours=i), uid=2 + i,
                )
            )

    seen: list[uuid.UUID] = []
    cursor: uuid.UUID | None = None
    for _ in range(20):
        page = await list_unified_messages(folder_name="Inbox", before=cursor, limit=1)
        seen.extend(m.id for m in page.messages)
        if not page.has_more:
            break
        assert page.next_cursor is not None
        cursor = uuid.UUID(page.next_cursor)

    assert len(seen) == len(expected_ids)
    assert set(seen) == expected_ids
