"""
Bulk-moving several messages into the glacier at once, through the same
POST .../bulk-action a "select all, archive" or "empty this folder"
flow in the UI drives.

Skips itself against the pinned default PostIMAP image the same way
tests/pg/test_glacier_api_pg.py does -- see that file's own docstring.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.mails import bulk_action as api_bulk_action
from mail_verdict.api.schemas import BulkActionRequest
from mail_verdict.database.connection import DatabaseConnection
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


async def _seed_account_with_messages(
    session: AsyncSession, *, count: int,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    """One account with glacier enabled, one archive folder, `count`
    eligible messages in it. Returns (account_id, folder_id,
    glacier_folder_id, [message_id, ...])."""
    account_id = uuid.uuid4()
    folder_id = uuid.uuid4()
    glacier_folder_id = uuid.uuid4()
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
            "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id) "
            "VALUES (:account_id, true, :glacier_folder_id)"
        ),
        {"account_id": account_id, "glacier_folder_id": glacier_folder_id},
    )
    message_ids = [uuid.uuid4() for _ in range(count)]
    for message_id in message_ids:
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
                " from_addr, raw_source, size_bytes, received_at) "
                "VALUES (:id, :account_id, :folder_id, :uid, :thread_id, :message_id_hdr, "
                " 'Bulk test', 'sender@example.com', :raw_source, :size_bytes, :received_at)"
            ),
            {
                "id": message_id, "account_id": account_id, "folder_id": folder_id,
                "uid": message_ids.index(message_id) + 1,
                "thread_id": message_id, "message_id_hdr": f"<{message_id}@example.com>",
                "raw_source": _RAW_SOURCE, "size_bytes": len(_RAW_SOURCE),
                "received_at": datetime.now(timezone.utc) - timedelta(days=400),
            },
        )
    return account_id, folder_id, glacier_folder_id, message_ids


@pytest.mark.asyncio
async def test_bulk_move_into_glacier(migrated_db: DatabaseConnection) -> None:
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        account_id, _folder_id, glacier_folder_id, message_ids = (
            await _seed_account_with_messages(session, count=3)
        )
        await session.commit()

    response = await api_bulk_action(
        account_id,
        BulkActionRequest(action="move", target_folder_id=glacier_folder_id, ids=message_ids),
    )

    assert response.success is True, response.errors
    assert response.affected_count == 3
    assert response.errors == []
    assert response.target_folder_id == glacier_folder_id

    async with migrated_db.session() as session:
        live = (
            await session.execute(
                text("SELECT count(*) FROM messages WHERE id = ANY(:ids) AND expunged_at IS NULL"),
                {"ids": message_ids},
            )
        ).scalar_one()
        assert live == 0
        glaciered = (
            await session.execute(
                text(
                    "SELECT count(*) FROM glacier_messages "
                    "WHERE origin_message_id = ANY(:ids) AND visible_at IS NOT NULL"
                ),
                {"ids": message_ids},
            )
        ).scalar_one()
        assert glaciered == 3


@pytest.mark.asyncio
async def test_bulk_move_into_another_accounts_glacier_is_refused(
    migrated_db: DatabaseConnection,
) -> None:
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        account_id, _folder_id, _own_glacier, message_ids = (
            await _seed_account_with_messages(session, count=1)
        )
        _other_account_id, _other_folder_id, other_glacier_folder_id, _other_ids = (
            await _seed_account_with_messages(session, count=0)
        )
        await session.commit()

    with pytest.raises(Exception) as exc_info:  # noqa: PT011
        await api_bulk_action(
            account_id,
            BulkActionRequest(
                action="move", target_folder_id=other_glacier_folder_id, ids=message_ids,
            ),
        )
    assert "does not belong to this account" in str(exc_info.value)
