"""
resolve_by_msg_key: the one resolver from a message's durable
(account_id, msg_key) identity to wherever it lives right now -- a live
row, a visible glacier row, or neither. Glaciering through the real
glacier_message_now flow, not a hand-seeded glacier_messages row, per
the same reasoning tests/pg/test_glacier_old_id_resolution_pg.py
already documents.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.msg_key import resolve_by_msg_key
from mail_verdict.glacier.operations import glacier_message_now
from mail_verdict.postimap.contract import read_postimap_info, supports_message_append

_RAW_SOURCE = b"From: sender@example.com\r\nSubject: Test\r\n\r\nBody\r\n"


async def _skip_unless_append_capable(db: DatabaseConnection) -> None:
    async with db.session() as session:
        info = await read_postimap_info(session)
    if info is None or not supports_message_append(info):
        pytest.skip(
            'this PostIMAP build does not carry outbox kind="append" -- '
            f"reports service_version={info.service_version if info else 'unknown'}, "
            "so glacier_message_now is correctly refused rather than exercised here"
        )


async def _seed_ready_message(
    db: DatabaseConnection,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
    account_id = uuid.uuid4()
    folder_id = uuid.uuid4()
    message_id = uuid.uuid4()
    message_id_hdr = f"<{message_id}@example.com>"
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO accounts "
                "(id, name, imap_host, imap_port, imap_user, imap_password) "
                "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
                "'\\x00' || convert_to('pw', 'UTF8'))"
            ),
            {"id": account_id, "name": f"acct-{account_id}"},
        )
        await session.execute(
            text(
                "INSERT INTO folders (id, account_id, imap_name, special_use, "
                "initial_sync_done) VALUES (:id, :account_id, 'Archive', 'archive', true)"
            ),
            {"id": folder_id, "account_id": account_id},
        )
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
                " from_addr, raw_source, size_bytes, received_at) "
                "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :message_id_hdr, "
                " 'Test', 'sender@example.com', :raw_source, :size_bytes, "
                " now() - interval '400 days')"
            ),
            {
                "id": message_id, "account_id": account_id, "folder_id": folder_id,
                "thread_id": message_id, "message_id_hdr": message_id_hdr,
                "raw_source": _RAW_SOURCE, "size_bytes": len(_RAW_SOURCE),
            },
        )
        await session.execute(
            text(
                "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id) "
                "VALUES (:account_id, true, :glacier_folder_id)"
            ),
            {"account_id": account_id, "glacier_folder_id": uuid.uuid4()},
        )
    return account_id, folder_id, message_id, message_id_hdr


@pytest.mark.asyncio
async def test_resolves_a_live_message(migrated_db: DatabaseConnection) -> None:
    account_id, folder_id, message_id, message_id_hdr = await _seed_ready_message(migrated_db)

    async with migrated_db.session() as session:
        found = await resolve_by_msg_key(
            session, account_id=account_id, msg_key=message_id_hdr,
        )

    assert found is not None
    assert found.kind == "live"
    assert found.id == message_id
    assert found.folder_id == folder_id


@pytest.mark.asyncio
async def test_resolves_a_glaciered_message(migrated_db: DatabaseConnection) -> None:
    await _skip_unless_append_capable(migrated_db)
    account_id, _folder_id, message_id, message_id_hdr = await _seed_ready_message(migrated_db)

    outcome = await glacier_message_now(migrated_db, message_id)
    assert outcome.ok, outcome.reason

    async with migrated_db.session() as session:
        glacier_folder_id = (
            await session.execute(
                text("SELECT glacier_folder_id FROM account_prefs WHERE account_id = :id"),
                {"id": account_id},
            )
        ).scalar_one()
        found = await resolve_by_msg_key(
            session, account_id=account_id, msg_key=message_id_hdr,
        )

    assert found is not None
    assert found.kind == "glacier"
    assert found.id == outcome.glacier_id
    assert found.folder_id == glacier_folder_id


@pytest.mark.asyncio
async def test_an_unknown_key_resolves_to_nothing(migrated_db: DatabaseConnection) -> None:
    account_id, _folder_id, _message_id, _hdr = await _seed_ready_message(migrated_db)

    async with migrated_db.session() as session:
        found = await resolve_by_msg_key(
            session, account_id=account_id, msg_key="<never-existed@example.com>",
        )

    assert found is None
