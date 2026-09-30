"""
mint_selection -- the "select all matching" snapshot GET /accounts/{id}/
messages/selection mints -- against the glacier's own synthetic folder
id. Its live-only query always answered zero for that id (never a row
in `messages`), which would make every bulk action against the whole
glacier refuse itself as an unconfirmed count. Checked for the same
fault: accounts.py's list_folders, folder_management.py's
_get_folders_with_counts/_fetch_folder_response/delete_folder, and
unified.py's list_unified_folders -- each already appends or unions the
glacier's own count from glacier_messages, and
delete_folder refuses a glacier id outright before ever reaching a
count query. database/repository.py's MessageRepository.get_by_folder
and .get_by_folder_and_uid share the same live-only pattern but are
never called from anywhere in the codebase.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from mail_verdict.api.mails import mint_selection
from mail_verdict.database.connection import DatabaseConnection

_RAW_SOURCE = b"From: sender@example.com\r\nSubject: Test\r\n\r\nBody\r\n"


async def _seed_glacier_messages(
    db: DatabaseConnection, *, count: int, unread_count: int,
) -> tuple[uuid.UUID, uuid.UUID]:
    """`count` visible glacier rows, `unread_count` of them unread.
    Returns (account_id, glacier_folder_id)."""
    account_id = uuid.uuid4()
    glacier_folder_id = uuid.uuid4()
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
                "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id) "
                "VALUES (:account_id, true, :glacier_folder_id)"
            ),
            {"account_id": account_id, "glacier_folder_id": glacier_folder_id},
        )
        for i in range(count):
            glacier_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO glacier_messages "
                    "(id, account_id, folder_id, thread_id, message_id, subject, from_addr, "
                    " to_addrs, body_text, raw_source, size_bytes, received_at, is_seen, "
                    " msg_key, state, visible_at, glaciered_at) "
                    "VALUES (:id, :account_id, :folder_id, :thread_id, :message_id_hdr, "
                    " 'Selection test', 'sender@example.com', '[\"me@example.com\"]', "
                    " 'Body', :raw_source, :size_bytes, now(), :is_seen, "
                    " :msg_key, 'glaciered', now(), now())"
                ),
                {
                    "id": glacier_id, "account_id": account_id, "folder_id": glacier_folder_id,
                    "thread_id": glacier_id, "message_id_hdr": f"<{glacier_id}@example.com>",
                    "raw_source": _RAW_SOURCE, "size_bytes": len(_RAW_SOURCE),
                    "is_seen": i >= unread_count, "msg_key": f"msg-{glacier_id}",
                },
            )
    return account_id, glacier_folder_id


@pytest.mark.asyncio
async def test_selection_count_over_the_whole_glacier(migrated_db: DatabaseConnection) -> None:
    account_id, glacier_folder_id = await _seed_glacier_messages(
        migrated_db, count=5, unread_count=2,
    )

    snapshot_all = await mint_selection(account_id, folder_id=glacier_folder_id, filter="all")
    assert snapshot_all.count == 5

    snapshot_unread = await mint_selection(
        account_id, folder_id=glacier_folder_id, filter="unread",
    )
    assert snapshot_unread.count == 2


@pytest.mark.asyncio
async def test_selection_count_ignores_another_accounts_glacier(
    migrated_db: DatabaseConnection,
) -> None:
    account_id, glacier_folder_id = await _seed_glacier_messages(
        migrated_db, count=3, unread_count=0,
    )
    other_account_id, _other_folder_id = await _seed_glacier_messages(
        migrated_db, count=7, unread_count=0,
    )

    snapshot = await mint_selection(account_id, folder_id=glacier_folder_id, filter="all")
    assert snapshot.count == 3

    other_snapshot = await mint_selection(
        other_account_id, folder_id=glacier_folder_id, filter="all",
    )
    assert other_snapshot.count == 0, "another account's glacier folder id names nothing here"
