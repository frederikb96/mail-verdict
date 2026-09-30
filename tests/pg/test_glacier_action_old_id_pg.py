"""
Acting on a glaciered message under the id a client held before it was
glaciered -- single action and bulk by explicit id alike -- through the
real glacier_message_now flow, not a hand-seeded glacier_messages row.

Every READ path already resolves this id (tests/pg/test_glacier_old_id_
resolution_pg.py); the per-message and bulk ACTION paths did not.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from mail_verdict.api.mails import bulk_action as api_bulk_action
from mail_verdict.api.mails import message_action as api_message_action
from mail_verdict.api.schemas import BulkActionRequest, MessageActionRequest
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.glacier.operations import glacier_message_now
from mail_verdict.postimap.contract import read_postimap_info, supports_message_append

_RAW_SOURCE = b"From: sender@example.com\r\nSubject: Test\r\n\r\nBody\r\n"


async def _skip_unless_append_capable(db: DatabaseConnection) -> None:
    async with db.session() as session:
        info = await read_postimap_info(session)
    if info is None or not supports_message_append(info):
        pytest.skip(
            'this PostIMAP build does not carry outbox kind="append" -- '
            f"reports service_version={info.service_version if info else 'unknown'}"
        )


async def _seed_account(db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID]:
    """One account, glacier enabled, one archive folder. Returns
    (account_id, folder_id)."""
    account_id = uuid.uuid4()
    folder_id = uuid.uuid4()
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
                "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id) "
                "VALUES (:account_id, true, :glacier_folder_id)"
            ),
            {"account_id": account_id, "glacier_folder_id": uuid.uuid4()},
        )
    return account_id, folder_id


async def _seed_ready_message(
    db: DatabaseConnection, *, account_id: uuid.UUID, folder_id: uuid.UUID, uid: int = 1,
) -> uuid.UUID:
    message_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
                " from_addr, raw_source, size_bytes, received_at) "
                "VALUES (:id, :account_id, :folder_id, :uid, :thread_id, :message_id_hdr, "
                " 'Test', 'sender@example.com', :raw_source, :size_bytes, :received_at)"
            ),
            {
                "id": message_id, "account_id": account_id, "folder_id": folder_id, "uid": uid,
                "thread_id": message_id, "message_id_hdr": f"<{message_id}@example.com>",
                "raw_source": _RAW_SOURCE, "size_bytes": len(_RAW_SOURCE),
                "received_at": datetime.now(timezone.utc) - timedelta(days=400),
            },
        )
    return message_id


@pytest.mark.asyncio
async def test_single_action_under_the_original_id(migrated_db: DatabaseConnection) -> None:
    await _skip_unless_append_capable(migrated_db)
    account_id, folder_id = await _seed_account(migrated_db)
    message_id = await _seed_ready_message(migrated_db, account_id=account_id, folder_id=folder_id)

    outcome = await glacier_message_now(migrated_db, message_id)
    assert outcome.ok, outcome.reason

    response = await api_message_action(
        message_id, MessageActionRequest(action="mark_read"),
    )
    assert response.success is True, response.message
    assert response.message_id == outcome.glacier_id

    async with migrated_db.session() as session:
        is_seen = (
            await session.execute(
                text("SELECT is_seen FROM glacier_messages WHERE id = :id"),
                {"id": outcome.glacier_id},
            )
        ).scalar_one()
    assert is_seen is True


@pytest.mark.asyncio
async def test_expunge_under_the_original_id_still_needs_confirmation(
    migrated_db: DatabaseConnection,
) -> None:
    await _skip_unless_append_capable(migrated_db)
    account_id, folder_id = await _seed_account(migrated_db)
    message_id = await _seed_ready_message(migrated_db, account_id=account_id, folder_id=folder_id)

    outcome = await glacier_message_now(migrated_db, message_id)
    assert outcome.ok, outcome.reason

    unconfirmed = await api_message_action(message_id, MessageActionRequest(action="expunge"))
    assert unconfirmed.success is False

    async with migrated_db.session() as session:
        still_there = (
            await session.execute(
                text("SELECT 1 FROM glacier_messages WHERE id = :id"),
                {"id": outcome.glacier_id},
            )
        ).scalar_one_or_none()
    assert still_there is not None

    confirmed = await api_message_action(
        message_id, MessageActionRequest(action="expunge", confirm=True),
    )
    assert confirmed.success is True, confirmed.message


@pytest.mark.asyncio
async def test_bulk_action_by_explicit_original_ids(migrated_db: DatabaseConnection) -> None:
    await _skip_unless_append_capable(migrated_db)
    account_id, folder_id = await _seed_account(migrated_db)
    message_id_1 = await _seed_ready_message(
        migrated_db, account_id=account_id, folder_id=folder_id, uid=1,
    )
    message_id_2 = await _seed_ready_message(
        migrated_db, account_id=account_id, folder_id=folder_id, uid=2,
    )

    outcome_1 = await glacier_message_now(migrated_db, message_id_1)
    assert outcome_1.ok, outcome_1.reason
    outcome_2 = await glacier_message_now(migrated_db, message_id_2)
    assert outcome_2.ok, outcome_2.reason

    response = await api_bulk_action(
        account_id,
        BulkActionRequest(action="mark_read", ids=[message_id_1, message_id_2]),
    )
    assert response.success is True, response.errors
    assert response.affected_count == 2

    async with migrated_db.session() as session:
        unread = (
            await session.execute(
                text(
                    "SELECT count(*) FROM glacier_messages "
                    "WHERE id = ANY(:ids) AND is_seen = false"
                ),
                {"ids": [outcome_1.glacier_id, outcome_2.glacier_id]},
            )
        ).scalar_one()
    assert unread == 0


@pytest.mark.asyncio
async def test_an_action_after_restore_is_refused_with_a_reason(
    migrated_db: DatabaseConnection,
) -> None:
    """The other direction: once a glacier row has been restored (a
    tombstone -- restored_at set, visible_at cleared), acting on it under
    its own id is refused with a reason a client can act on, rather than
    silently redirected onto the now-live restored message under an id
    the caller never asked for."""
    await _skip_unless_append_capable(migrated_db)
    account_id, folder_id = await _seed_account(migrated_db)
    message_id = await _seed_ready_message(migrated_db, account_id=account_id, folder_id=folder_id)

    outcome = await glacier_message_now(migrated_db, message_id)
    assert outcome.ok, outcome.reason
    assert outcome.glacier_id is not None

    # A genuine tombstone needs the appended copy to actually land and
    # sync back -- confirm_restores's own job, and only a real mail
    # server (tests/e2e/test_glacier_restore_flow.py) can complete that.
    # The action-refusal logic under test only cares about the terminal
    # DB state a completed restore leaves behind (restored_at set,
    # visible_at cleared -- GlacierMessage's own docstring), so that
    # state is set directly here rather than driven through the whole
    # outbox/APPEND/sync mechanism this pg-only test has no server for.
    async with migrated_db.session() as session:
        await session.execute(
            text(
                "UPDATE glacier_messages SET restored_at = now(), visible_at = NULL "
                "WHERE id = :id"
            ),
            {"id": outcome.glacier_id},
        )

    response = await api_message_action(
        outcome.glacier_id, MessageActionRequest(action="mark_read"),
    )
    assert response.success is False
    assert response.message is not None and "restored" in response.message
