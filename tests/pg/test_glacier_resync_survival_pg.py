"""
A fully glaciered message survives a UIDVALIDITY resync of its account
untouched (row 8.1's own acceptance criterion: "a message moved to the
glacier ... survives a full resync of its account untouched").

A UIDVALIDITY change makes PostIMAP delete and recreate every folder
and message row for the affected mailbox, assigning fresh ids
(design section 3.8) -- simulated here directly with SQL, the same
shape PostIMAP's own resync takes, since provoking a genuine
UIDVALIDITY change against a real IMAP server is not something this
layer can arrange. A *visible* glacier row (state='glaciered',
raw_source populated) never reads origin_message_id/origin_folder_id
again once it reaches that state -- those are join hints for
mid-flight rows only (glacier/operations.py's reresolve_origins) -- so
nothing about it depends on the old ids surviving at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.mails import get_message, list_message_page
from mail_verdict.database.connection import DatabaseConnection

_RAW_SOURCE = b"From: sender@example.com\r\nSubject: Test\r\n\r\nBody\r\n"


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


async def _seed_folder(
    session: AsyncSession, account_id: uuid.UUID, *, uidvalidity: int,
) -> uuid.UUID:
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use, initial_sync_done, "
            "uidvalidity) VALUES (:id, :account_id, 'Archive', 'archive', true, :uidvalidity)"
        ),
        {"id": folder_id, "account_id": account_id, "uidvalidity": uidvalidity},
    )
    return folder_id


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


async def _seed_visible_glacier_message(
    session: AsyncSession, *, account_id: uuid.UUID, glacier_folder_id: uuid.UUID,
    origin_message_id: uuid.UUID, origin_folder_id: uuid.UUID,
) -> uuid.UUID:
    """A fully glaciered (terminal) row -- state='glaciered', raw_source
    populated, still naming the pre-resync origin ids as stale hints."""
    glacier_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO glacier_messages "
            "(id, account_id, folder_id, thread_id, message_id, subject, from_addr, "
            " raw_source, size_bytes, received_at, msg_key, state, visible_at, "
            " glaciered_at, origin_message_id, origin_folder_id, origin_imap_name) "
            "VALUES (:id, :account_id, :folder_id, :thread_id, :message_id_hdr, "
            " 'Survives resync', 'sender@example.com', :raw_source, :size_bytes, "
            " :received_at, :msg_key, 'glaciered', now(), now(), "
            " :origin_message_id, :origin_folder_id, 'Archive')"
        ),
        {
            "id": glacier_id, "account_id": account_id, "folder_id": glacier_folder_id,
            "thread_id": glacier_id, "message_id_hdr": f"<{glacier_id}@example.com>",
            "raw_source": _RAW_SOURCE, "size_bytes": len(_RAW_SOURCE),
            "received_at": datetime.now(timezone.utc) - timedelta(days=400),
            "msg_key": f"msg-{glacier_id}",
            "origin_message_id": origin_message_id, "origin_folder_id": origin_folder_id,
        },
    )
    return glacier_id


async def _simulate_uidvalidity_resync(
    session: AsyncSession, *, account_id: uuid.UUID, old_folder_id: uuid.UUID,
) -> uuid.UUID:
    """The shape design section 3.8 describes: PostIMAP deletes and
    recreates every folder (and, via ON DELETE CASCADE, every message)
    for the account under a new UIDVALIDITY, with fresh ids throughout.
    Returns the new folder id."""
    await session.execute(text("DELETE FROM folders WHERE id = :id"), {"id": old_folder_id})
    new_folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use, initial_sync_done, "
            "uidvalidity) VALUES (:id, :account_id, 'Archive', 'archive', true, 999)"
        ),
        {"id": new_folder_id, "account_id": account_id},
    )
    return new_folder_id


@pytest.mark.asyncio
async def test_a_fully_glaciered_message_survives_a_uidvalidity_resync(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        glacier_folder_id = await _enable_glacier(session, account_id)
        old_folder_id = await _seed_folder(session, account_id, uidvalidity=1)
        # The live message this glacier row's own origin hints named,
        # standing in for what a real copy_message/expunge_message
        # sequence would have expunged already -- gone by the time a
        # resync happens for a fully glaciered row, same as production.
        origin_message_id = uuid.uuid4()
        glacier_id = await _seed_visible_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            origin_message_id=origin_message_id, origin_folder_id=old_folder_id,
        )
        await session.commit()

    async with migrated_db.session() as session:
        await _simulate_uidvalidity_resync(
            session, account_id=account_id, old_folder_id=old_folder_id,
        )
        await session.commit()

    # Still exactly the row it was: nothing about a visible glacier row
    # depends on the folder/message ids a resync just replaced.
    async with migrated_db.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT state, visible_at, raw_source, subject, folder_id "
                    "FROM glacier_messages WHERE id = :id"
                ),
                {"id": glacier_id},
            )
        ).mappings().one()
    assert row["state"] == "glaciered"
    assert row["visible_at"] is not None
    assert row["raw_source"] == _RAW_SOURCE
    assert row["subject"] == "Survives resync"
    assert row["folder_id"] == glacier_folder_id

    # Still listed in the glacier folder.
    async with migrated_db.session() as session:
        page = await list_message_page(
            session, account_id=account_id, folder_id=glacier_folder_id, folder_scope=None,
            threaded=False, is_seen=None, since=None, before=None, after=None, around=None,
            limit=50,
        )
    assert [m.id for m in page.messages] == [glacier_id]
    assert page.messages[0].is_glacier is True

    # Still readable in full.
    detail = await get_message(glacier_id)
    assert detail.subject == "Survives resync"
    assert detail.is_glacier is True
