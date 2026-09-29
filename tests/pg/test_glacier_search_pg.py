"""
Text search over the glacier (design section 4.4): a glaciered message
is found by the primary tsquery stage, contributes to the date-range
axis, and a search scoped to nothing (no folder_ids at all) reaches it
the same as one scoped to its own account.

Glacier rows here are inserted directly with search_vector left to its
own generated-column expression (migration 0034_glacier replicates
PostIMAP's verbatim) -- see test_glacier_listing_pg.py's own docstring
for why a raw insert is the right fixture for a read-path test like
this one.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.search import search_date_bounds, search_messages
from mail_verdict.database.connection import DatabaseConnection


async def _seed_account(session: AsyncSession) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts "
            "(id, name, imap_host, imap_port, imap_user, imap_password, is_active) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'), false)"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    return account_id


async def _enable_glacier(session: AsyncSession, account_id: uuid.UUID) -> uuid.UUID:
    glacier_folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id) "
            "VALUES (:account_id, true, :glacier_folder_id)"
        ),
        {"account_id": account_id, "glacier_folder_id": glacier_folder_id},
    )
    return glacier_folder_id


async def _seed_folder(session: AsyncSession, account_id: uuid.UUID) -> uuid.UUID:
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use, initial_sync_done) "
            "VALUES (:id, :account_id, 'INBOX', NULL, true)"
        ),
        {"id": folder_id, "account_id": account_id},
    )
    return folder_id


async def _seed_live_message(
    session: AsyncSession, *, account_id: uuid.UUID, folder_id: uuid.UUID,
    subject: str, received_at: datetime,
) -> uuid.UUID:
    message_id = uuid.uuid4()
    raw = b"From: sender@example.com\r\nSubject: Test\r\n\r\nBody\r\n"
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
            " from_addr, raw_source, size_bytes, received_at) "
            "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :message_id_hdr, :subject, "
            " 'sender@example.com', :raw_source, :size_bytes, :received_at)"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": folder_id,
            "thread_id": message_id, "message_id_hdr": f"<{message_id}@example.com>",
            "subject": subject, "raw_source": raw, "size_bytes": len(raw),
            "received_at": received_at,
        },
    )
    return message_id


async def _seed_glacier_message(
    session: AsyncSession, *, account_id: uuid.UUID, glacier_folder_id: uuid.UUID,
    subject: str, received_at: datetime,
) -> uuid.UUID:
    glacier_id = uuid.uuid4()
    raw = b"From: sender@example.com\r\nSubject: Test\r\n\r\nBody\r\n"
    await session.execute(
        text(
            "INSERT INTO glacier_messages "
            "(id, account_id, folder_id, thread_id, message_id, subject, from_addr, "
            " raw_source, size_bytes, received_at, msg_key, state, visible_at, glaciered_at) "
            "VALUES (:id, :account_id, :folder_id, :thread_id, :message_id_hdr, :subject, "
            " 'sender@example.com', :raw_source, :size_bytes, :received_at, :msg_key, "
            " 'glaciered', now(), now())"
        ),
        {
            "id": glacier_id, "account_id": account_id, "folder_id": glacier_folder_id,
            "thread_id": glacier_id, "message_id_hdr": f"<{glacier_id}@example.com>",
            "subject": subject, "raw_source": raw, "size_bytes": len(raw),
            "received_at": received_at, "msg_key": f"msg-{glacier_id}",
        },
    )
    return glacier_id


@pytest.mark.asyncio
async def test_a_glaciered_message_is_found_scoped_to_its_account(
    migrated_db: DatabaseConnection,
) -> None:
    now = datetime.now(timezone.utc)
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        glacier_folder_id = await _enable_glacier(session, account_id)
        live_folder_id = await _seed_folder(session, account_id)
        live_id = await _seed_live_message(
            session, account_id=account_id, folder_id=live_folder_id,
            subject="Ordinary quarterly report", received_at=now,
        )
        glacier_id = await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            subject="Archived quarterly figures", received_at=now - timedelta(days=400),
        )

    page = await search_messages(
        q="quarterly", account_id=account_id, folder_ids=None,
        fields=["subject"], before=None, limit=50,
    )
    ids = {r.id for r in page.results}
    assert ids == {live_id, glacier_id}
    glacier_result = next(r for r in page.results if r.id == glacier_id)
    assert glacier_result.is_glacier is True
    assert glacier_result.pending_sync is False
    live_result = next(r for r in page.results if r.id == live_id)
    assert live_result.is_glacier is False


@pytest.mark.asyncio
async def test_a_glaciered_message_is_found_scoped_to_nothing(
    migrated_db: DatabaseConnection,
) -> None:
    """No account_id, no folder_ids -- an instance-wide search, the
    widest scope search offers."""
    now = datetime.now(timezone.utc)
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        glacier_folder_id = await _enable_glacier(session, account_id)
        glacier_id = await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            subject="Unscoped needle findme12345", received_at=now,
        )

    page = await search_messages(
        q="findme12345", account_id=None, folder_ids=None,
        fields=["subject"], before=None, limit=50,
    )
    assert glacier_id in {r.id for r in page.results}


@pytest.mark.asyncio
async def test_search_scoped_to_the_glacier_folder_alone(
    migrated_db: DatabaseConnection,
) -> None:
    now = datetime.now(timezone.utc)
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        glacier_folder_id = await _enable_glacier(session, account_id)
        live_folder_id = await _seed_folder(session, account_id)
        await _seed_live_message(
            session, account_id=account_id, folder_id=live_folder_id,
            subject="Overlap term abcdefgh", received_at=now,
        )
        glacier_id = await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            subject="Overlap term abcdefgh", received_at=now,
        )

    page = await search_messages(
        q="abcdefgh", account_id=account_id, folder_ids=[glacier_folder_id],
        fields=["subject"], before=None, limit=50,
    )
    assert {r.id for r in page.results} == {glacier_id}


@pytest.mark.asyncio
async def test_date_bounds_include_the_glacier(migrated_db: DatabaseConnection) -> None:
    now = datetime.now(timezone.utc)
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        glacier_folder_id = await _enable_glacier(session, account_id)
        live_folder_id = await _seed_folder(session, account_id)
        await _seed_live_message(
            session, account_id=account_id, folder_id=live_folder_id,
            subject="Newer", received_at=now,
        )
        await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            subject="Much older", received_at=now - timedelta(days=1000),
        )

    bounds = await search_date_bounds(account_id, folder_ids=None)
    assert bounds.oldest is not None
    assert (now - bounds.oldest).days >= 999


@pytest.mark.asyncio
async def test_a_search_cursor_can_land_on_a_glaciered_result(
    migrated_db: DatabaseConnection,
) -> None:
    """Paging with `before` set to a glacier id must resolve it --
    resolve_search_cursor tries messages first, then glacier_messages
    (design section 4.4)."""
    now = datetime.now(timezone.utc)
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        glacier_folder_id = await _enable_glacier(session, account_id)
        live_folder_id = await _seed_folder(session, account_id)
        newest_id = await _seed_live_message(
            session, account_id=account_id, folder_id=live_folder_id,
            subject="Cursor term cursorword newest", received_at=now,
        )
        cursor_glacier_id = await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            subject="Cursor term cursorword middle", received_at=now - timedelta(days=1),
        )
        oldest_id = await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            subject="Cursor term cursorword oldest", received_at=now - timedelta(days=2),
        )

    next_page = await search_messages(
        q="cursorword", account_id=account_id, folder_ids=None,
        fields=["subject"], before=cursor_glacier_id, limit=50,
    )
    ids = {r.id for r in next_page.results}
    assert ids == {oldest_id}
    assert newest_id not in ids
    assert cursor_glacier_id not in ids


@pytest.mark.asyncio
async def test_a_real_folder_search_never_reaches_the_glacier(
    migrated_db: DatabaseConnection,
) -> None:
    now = datetime.now(timezone.utc)
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        glacier_folder_id = await _enable_glacier(session, account_id)
        live_folder_id = await _seed_folder(session, account_id)
        live_id = await _seed_live_message(
            session, account_id=account_id, folder_id=live_folder_id,
            subject="Scoped term xyzscoped", received_at=now,
        )
        await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            subject="Scoped term xyzscoped", received_at=now,
        )

    page = await search_messages(
        q="xyzscoped", account_id=account_id, folder_ids=[live_folder_id],
        fields=["subject"], before=None, limit=50,
    )
    assert {r.id for r in page.results} == {live_id}
