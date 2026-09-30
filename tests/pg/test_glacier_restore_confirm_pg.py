"""
confirm_restores must be confirmed by the very outbox entry a restore
created -- tied to that entry's own PostIMAP-reported outcome and to
byte-identical content -- never by matching a live message that merely
looks similar (same size, or even the same Message-ID header) while the
restore's own outbox entry has not yet reported success. A restore is
not observed anywhere on the server before that; confirming early
destroys the glacier's bytes while the message may be readable nowhere
at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from mail_verdict.api.mails import locate_message
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.glacier.operations import confirm_or_withdraw_removing, glacier_message_now
from mail_verdict.glacier.restore import confirm_restores, fail_stale_restores, start_restore
from mail_verdict.postimap.contract import read_postimap_info, supports_message_append

_RAW_SOURCE = b"From: sender@example.com\r\nSubject: Test\r\n\r\nBody\r\n"
# Same length as _RAW_SOURCE, different bytes -- a message that would
# have matched the old "same size" heuristic without being the same
# message at all.
_LOOKALIKE_SOURCE = b"From: sender@example.com\r\nSubject: Test\r\n\r\nDoy!\r\n"
assert len(_LOOKALIKE_SOURCE) == len(_RAW_SOURCE)


async def _skip_unless_append_capable(db: DatabaseConnection) -> None:
    async with db.session() as session:
        info = await read_postimap_info(session)
    if info is None or not supports_message_append(info):
        pytest.skip(
            'this PostIMAP build does not carry outbox kind="append" -- '
            f"reports service_version={info.service_version if info else 'unknown'}"
        )


async def _seed_account(db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """One account, glacier enabled, an archive folder to glacier from and
    a separate target folder to restore into. Returns (account_id,
    archive_folder_id, target_folder_id)."""
    account_id = uuid.uuid4()
    archive_folder_id = uuid.uuid4()
    target_folder_id = uuid.uuid4()
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
            {"id": archive_folder_id, "account_id": account_id},
        )
        await session.execute(
            text(
                "INSERT INTO folders (id, account_id, imap_name, initial_sync_done) "
                "VALUES (:id, :account_id, 'INBOX', true)"
            ),
            {"id": target_folder_id, "account_id": account_id},
        )
        await session.execute(
            text(
                "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id) "
                "VALUES (:account_id, true, :glacier_folder_id)"
            ),
            {"account_id": account_id, "glacier_folder_id": uuid.uuid4()},
        )
    return account_id, archive_folder_id, target_folder_id


async def _seed_message(
    db: DatabaseConnection, *, account_id: uuid.UUID, folder_id: uuid.UUID, uid: int,
    message_id_hdr: str | None = None, imap_uid: int | None = None,
    raw_source: bytes = _RAW_SOURCE,
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
                "id": message_id, "account_id": account_id, "folder_id": folder_id,
                "uid": imap_uid if imap_uid is not None else uid,
                "thread_id": message_id, "message_id_hdr": message_id_hdr,
                "raw_source": raw_source, "size_bytes": len(raw_source),
                "received_at": datetime.now(timezone.utc) - timedelta(days=400),
            },
        )
    return message_id


async def _glacier_and_start_restore(
    db: DatabaseConnection, *, account_id: uuid.UUID, archive_id: uuid.UUID,
    target_id: uuid.UUID, raw_source: bytes = _RAW_SOURCE,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Glacier a message all the way to the terminal 'glaciered' state and
    start a restore. Returns (glacier_id, outbox_id)."""
    message_id = await _seed_message(
        db, account_id=account_id, folder_id=archive_id, uid=1, raw_source=raw_source,
    )
    outcome = await glacier_message_now(db, message_id)
    assert outcome.ok, outcome.reason
    glacier_id = outcome.glacier_id
    assert glacier_id is not None
    await confirm_or_withdraw_removing(db, account_id, grace_seconds=0)

    result = await start_restore(db, glacier_id, target_id)
    assert result.ok, result.reason
    assert result.outbox_id is not None
    return glacier_id, result.outbox_id


@pytest.mark.asyncio
async def test_restore_is_not_confirmed_while_the_outbox_entry_is_still_pending(
    migrated_db: DatabaseConnection,
) -> None:
    """The red team's reproduction: a same-size look-alike message sits in
    the target folder while the restore's own outbox entry is still
    pending. The old code matched on
    coalesce(message_id, '') = coalesce(g.message_id, '') plus size and
    confirmed against whichever look-alike it found first, deleting the
    glacier's raw source and attachments before any APPEND had landed."""
    await _skip_unless_append_capable(migrated_db)
    account_id, archive_id, target_id = await _seed_account(migrated_db)
    glacier_id, outbox_id = await _glacier_and_start_restore(
        migrated_db, account_id=account_id, archive_id=archive_id, target_id=target_id,
    )

    # A same-size look-alike, already sitting in the target folder --
    # what the old bug matched.
    await _seed_message(
        migrated_db, account_id=account_id, folder_id=target_id, uid=50,
        raw_source=_LOOKALIKE_SOURCE,
    )

    async with migrated_db.session() as session:
        # Force the precondition regardless of anything the real,
        # unreachable-server-bound PostIMAP container may have already
        # attempted -- the outbox entry has not yet reported success.
        await session.execute(
            text("UPDATE outbox SET status = 'pending' WHERE id = :id"), {"id": outbox_id},
        )

    confirmed = await confirm_restores(migrated_db, account_id)
    assert confirmed == 0

    async with migrated_db.session() as session:
        row = (
            await session.execute(
                text("SELECT state, raw_source FROM glacier_messages WHERE id = :id"),
                {"id": glacier_id},
            )
        ).mappings().one()
        assert row["state"] == "restoring"
        assert row["raw_source"] == _RAW_SOURCE


@pytest.mark.asyncio
async def test_restore_is_not_confirmed_by_a_same_size_different_content_lookalike(
    migrated_db: DatabaseConnection,
) -> None:
    """Even once the outbox entry reports success, a live message that
    merely happens to share its size (or a stale/reused Message-ID) but
    not its bytes must never confirm the restore -- only a byte-identical
    live row proves this is the copy this row's own APPEND produced."""
    await _skip_unless_append_capable(migrated_db)
    account_id, archive_id, target_id = await _seed_account(migrated_db)
    glacier_id, outbox_id = await _glacier_and_start_restore(
        migrated_db, account_id=account_id, archive_id=archive_id, target_id=target_id,
    )

    await _seed_message(
        migrated_db, account_id=account_id, folder_id=target_id, uid=50,
        raw_source=_LOOKALIKE_SOURCE,
    )

    async with migrated_db.session() as session:
        await session.execute(
            text("UPDATE outbox SET status = 'sent' WHERE id = :id"), {"id": outbox_id},
        )

    confirmed = await confirm_restores(migrated_db, account_id)
    assert confirmed == 0

    async with migrated_db.session() as session:
        row = (
            await session.execute(
                text("SELECT state, raw_source FROM glacier_messages WHERE id = :id"),
                {"id": glacier_id},
            )
        ).mappings().one()
        assert row["state"] == "restoring"
        assert row["raw_source"] == _RAW_SOURCE


@pytest.mark.asyncio
async def test_restore_confirms_once_a_byte_identical_live_row_exists_and_the_outbox_is_sent(
    migrated_db: DatabaseConnection,
) -> None:
    """Once PostIMAP reports the append landed (status='sent') and the
    live row byte-identical to the glacier's own stored copy exists on
    the server, the restore confirms: tags, verdicts and the tombstone
    all update."""
    await _skip_unless_append_capable(migrated_db)
    account_id, archive_id, target_id = await _seed_account(migrated_db)
    glacier_id, outbox_id = await _glacier_and_start_restore(
        migrated_db, account_id=account_id, archive_id=archive_id, target_id=target_id,
    )

    await _seed_message(
        migrated_db, account_id=account_id, folder_id=target_id, uid=77, imap_uid=77,
        raw_source=_RAW_SOURCE,
    )
    async with migrated_db.session() as session:
        await session.execute(
            text("UPDATE outbox SET status = 'sent' WHERE id = :id"), {"id": outbox_id},
        )

    confirmed = await confirm_restores(migrated_db, account_id)
    assert confirmed == 1

    async with migrated_db.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT restored_at, visible_at, raw_source FROM glacier_messages "
                    "WHERE id = :id"
                ),
                {"id": glacier_id},
            )
        ).mappings().one()
        assert row["restored_at"] is not None
        assert row["visible_at"] is None
        assert row["raw_source"] is None
        att_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM glacier_attachments WHERE glacier_message_id = :id"
                ),
                {"id": glacier_id},
            )
        ).scalar_one()
        assert att_count == 0


@pytest.mark.asyncio
async def test_locate_message_resolves_a_restored_tombstone_to_its_live_twin(
    migrated_db: DatabaseConnection,
) -> None:
    """locate_message's own restored-tombstone branch resolves through
    the durable-key resolver rather than a second hand-written lookup --
    proven by driving a real restore round trip and asking for the
    glacier row's own id afterward. Needs a real Message-ID header on
    both ends: the hash-fallback msg_key form the other tests in this
    file use never resolves through the resolver's live arm at all, by
    its own documented limit, which would pass this test for the wrong
    reason (a 404 neither before nor after this branch's rewrite could
    tell apart)."""
    await _skip_unless_append_capable(migrated_db)
    account_id, archive_id, target_id = await _seed_account(migrated_db)
    header = "<round-trip@example.com>"
    origin_id = await _seed_message(
        migrated_db, account_id=account_id, folder_id=archive_id, uid=1, message_id_hdr=header,
    )
    outcome = await glacier_message_now(migrated_db, origin_id)
    assert outcome.ok, outcome.reason
    glacier_id = outcome.glacier_id
    assert glacier_id is not None
    await confirm_or_withdraw_removing(migrated_db, account_id, grace_seconds=0)

    result = await start_restore(migrated_db, glacier_id, target_id)
    assert result.ok, result.reason
    outbox_id = result.outbox_id
    assert outbox_id is not None

    live_id = await _seed_message(
        migrated_db, account_id=account_id, folder_id=target_id, uid=77, imap_uid=77,
        message_id_hdr=header,
    )
    async with migrated_db.session() as session:
        await session.execute(
            text("UPDATE outbox SET status = 'sent' WHERE id = :id"), {"id": outbox_id},
        )
    assert await confirm_restores(migrated_db, account_id) == 1

    location = await locate_message(glacier_id)
    assert location.id == live_id
    assert location.account_id == account_id
    assert location.folder_id == target_id


@pytest.mark.asyncio
async def test_a_confirmed_restore_reaches_a_terminal_state_and_never_re_confirms(
    migrated_db: DatabaseConnection,
) -> None:
    """confirm_restores set restored_at but never changed state away from
    'restoring', so it kept matching its own candidate query and
    re-confirmed (re-announcing mail.updated + folder.changed) on every
    later tick, indefinitely."""
    await _skip_unless_append_capable(migrated_db)
    account_id, archive_id, target_id = await _seed_account(migrated_db)
    glacier_id, outbox_id = await _glacier_and_start_restore(
        migrated_db, account_id=account_id, archive_id=archive_id, target_id=target_id,
    )
    await _seed_message(
        migrated_db, account_id=account_id, folder_id=target_id, uid=77, imap_uid=77,
        raw_source=_RAW_SOURCE,
    )
    async with migrated_db.session() as session:
        await session.execute(
            text("UPDATE outbox SET status = 'sent' WHERE id = :id"), {"id": outbox_id},
        )
    assert await confirm_restores(migrated_db, account_id) == 1

    async with migrated_db.session() as session:
        row = (
            await session.execute(
                text("SELECT state FROM glacier_messages WHERE id = :id"), {"id": glacier_id},
            )
        ).mappings().one()
        assert row["state"] == "restored"

    # A further tick, with the exact same live row and outbox status
    # still in place, must change nothing -- the row no longer matches
    # confirm_restores's own state='restoring' candidate query.
    assert await confirm_restores(migrated_db, account_id) == 0


@pytest.mark.asyncio
async def test_the_restore_timeout_never_flips_a_completed_restore_to_failed(
    migrated_db: DatabaseConnection,
) -> None:
    """fail_stale_restores must never touch a row confirm_restores has
    already finished -- watched live, a restore completed 28 minutes
    earlier was flipped to failed with 'copy intact' logged while
    raw_source was actually NULL, and a retry then answered 'already
    restored once'."""
    await _skip_unless_append_capable(migrated_db)
    account_id, archive_id, target_id = await _seed_account(migrated_db)
    glacier_id, outbox_id = await _glacier_and_start_restore(
        migrated_db, account_id=account_id, archive_id=archive_id, target_id=target_id,
    )
    await _seed_message(
        migrated_db, account_id=account_id, folder_id=target_id, uid=77, imap_uid=77,
        raw_source=_RAW_SOURCE,
    )
    async with migrated_db.session() as session:
        await session.execute(
            text("UPDATE outbox SET status = 'sent' WHERE id = :id"), {"id": outbox_id},
        )
    assert await confirm_restores(migrated_db, account_id) == 1

    # timeout_seconds=0 is the harshest possible timeout -- if the
    # terminal state protects the row at all, this proves it.
    failed = await fail_stale_restores(migrated_db, account_id, timeout_seconds=0)
    assert failed == 0

    async with migrated_db.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT state, last_error FROM glacier_messages WHERE id = :id"
                ),
                {"id": glacier_id},
            )
        ).mappings().one()
        assert row["state"] == "restored"
        assert row["last_error"] is None
