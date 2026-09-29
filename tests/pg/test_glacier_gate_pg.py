"""
The glacier only works with a PostIMAP capable of the append outbox kind
restore needs -- moving a message in is refused on exactly the same
check restoring it back out is, since restore must work before removal
is ever offered at all. Run with no special image arrangement: this
proves the refusal against whatever PostIMAP is actually pinned for the
rest of the suite (currently 1.10.0, which does not carry the
capability), which is the case that must always hold in the ordinary
run. The allow path -- moving in and restoring actually succeeding on a
capable PostIMAP -- is proven by tests/pg/test_glacier_api_pg.py and
tests/e2e/test_glacier_restore_flow.py, run against a capable image via
MAIL_VERDICT_TEST_POSTIMAP_IMAGE (see tests/setup/images.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from mail_verdict.api.mails import message_action as api_message_action
from mail_verdict.api.schemas import MessageActionRequest
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.glacier.operations import glacier_message_now
from mail_verdict.glacier.restore import require_message_append_support
from mail_verdict.postimap.contract import MIN_MESSAGE_APPEND_SERVICE_VERSION

_RAW_SOURCE = b"From: sender@example.com\r\nSubject: Test\r\n\r\nBody\r\n"


async def _seed_ready_message(
    db: DatabaseConnection,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    account_id = uuid.uuid4()
    folder_id = uuid.uuid4()
    message_id = uuid.uuid4()
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
                " 'Test', 'sender@example.com', :raw_source, :size_bytes, :received_at)"
            ),
            {
                "id": message_id, "account_id": account_id, "folder_id": folder_id,
                "thread_id": message_id, "message_id_hdr": f"<{message_id}@example.com>",
                "raw_source": _RAW_SOURCE, "size_bytes": len(_RAW_SOURCE),
                "received_at": datetime.now(timezone.utc) - timedelta(days=400),
            },
        )
        await session.execute(
            text(
                "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id) "
                "VALUES (:account_id, true, :glacier_folder_id)"
            ),
            {"account_id": account_id, "glacier_folder_id": uuid.uuid4()},
        )
    return account_id, folder_id, message_id


@pytest.mark.asyncio
async def test_the_pinned_postimap_does_not_carry_the_capability(
    migrated_db: DatabaseConnection,
) -> None:
    """A sanity check on the fixture itself, so a false pass elsewhere --
    the gate reporting "refused" because it could not read postimap_info
    at all, say -- is never mistaken for the gate actually working."""
    error = await require_message_append_support(migrated_db)
    assert error is not None
    # Names the *running* version -- which must be below what the gate needs,
    # or this fixture no longer proves what the rest of the file assumes.
    assert "1.10.0" in error
    major, minor = MIN_MESSAGE_APPEND_SERVICE_VERSION[:2]
    assert (major, minor) > (1, 10)


@pytest.mark.asyncio
async def test_moving_into_the_glacier_is_refused_naming_the_version(
    migrated_db: DatabaseConnection,
) -> None:
    _, _, message_id = await _seed_ready_message(migrated_db)
    outcome = await glacier_message_now(migrated_db, message_id)
    assert outcome.ok is False
    assert outcome.reason is not None
    assert "1.11.0" in outcome.reason or "newer PostIMAP" in outcome.reason

    async with migrated_db.session() as session:
        live = (
            await session.execute(
                text("SELECT expunged_at FROM messages WHERE id = :id"), {"id": message_id},
            )
        ).mappings().one()
        assert live["expunged_at"] is None
        glacier_row = (
            await session.execute(
                text("SELECT 1 FROM glacier_messages WHERE origin_message_id = :id"),
                {"id": message_id},
            )
        ).scalar_one_or_none()
        assert glacier_row is None, "nothing should be copied either, when the gate refuses"


@pytest.mark.asyncio
async def test_the_move_action_refuses_the_same_way_through_the_api(
    migrated_db: DatabaseConnection,
) -> None:
    account_id, _, message_id = await _seed_ready_message(migrated_db)
    async with migrated_db.session() as session:
        glacier_folder_id = (
            await session.execute(
                text("SELECT glacier_folder_id FROM account_prefs WHERE account_id = :id"),
                {"id": account_id},
            )
        ).scalar_one()

    response = await api_message_action(
        message_id, MessageActionRequest(action="move", target_folder_id=glacier_folder_id),
    )
    assert response.success is False
    assert response.message is not None and "newer PostIMAP" in response.message
