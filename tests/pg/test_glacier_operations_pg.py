"""
The glacier's copy/verify/expunge sequence against a real Postgres
schema. Freddy intends to move thousands of real messages through this
path, so the destructive guards below are the tests this feature exists
for: a corrupted stored copy must never be followed by a removal, and a
removal must only ever be able to touch the exact message that was
copied and verified -- never a different one, even one deliberately
substituted.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.glacier.operations import (
    confirm_or_withdraw_removing,
    copy_message,
    eligibility_reason,
    expunge_message,
    glacier_message_now,
    reresolve_origins,
    resolve_duplicate,
    verify_message,
)
from mail_verdict.postimap.contract import read_postimap_info, supports_message_append

_RAW_SOURCE = b"From: sender@example.com\r\nSubject: Test\r\n\r\nBody\r\n"


async def _seed_account(session: AsyncSession, *, is_active: bool = True) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts "
            "(id, name, imap_host, imap_port, imap_user, imap_password, is_active) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'), :is_active)"
        ),
        {"id": account_id, "name": f"acct-{account_id}", "is_active": is_active},
    )
    return account_id


async def _seed_folder(
    session: AsyncSession, account_id: uuid.UUID, *, special_use: str | None = "archive",
    initial_sync_done: bool = True,
) -> uuid.UUID:
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use, initial_sync_done) "
            "VALUES (:id, :account_id, 'Archive', :special_use, :initial_sync_done)"
        ),
        {
            "id": folder_id, "account_id": account_id, "special_use": special_use,
            "initial_sync_done": initial_sync_done,
        },
    )
    return folder_id


async def _seed_message(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    folder_id: uuid.UUID,
    raw_source: bytes | None = _RAW_SOURCE,
    is_truncated: bool = False,
    imap_uid: int | None = 1,
    is_draft: bool = False,
    received_at: datetime | None = None,
    message_id_hdr: str | None = None,
) -> uuid.UUID:
    message_id = uuid.uuid4()
    hdr = message_id_hdr if message_id_hdr is not None else f"<{message_id}@example.com>"
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, from_addr, "
            " raw_source, is_truncated, is_draft, size_bytes, received_at) "
            "VALUES (:id, :account_id, :folder_id, :imap_uid, :thread_id, :message_id_hdr, "
            " 'Test', 'sender@example.com', :raw_source, :is_truncated, :is_draft, "
            " :size_bytes, :received_at)"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": folder_id,
            "imap_uid": imap_uid, "thread_id": message_id, "message_id_hdr": hdr,
            "raw_source": raw_source, "is_truncated": is_truncated, "is_draft": is_draft,
            "size_bytes": len(raw_source) if raw_source is not None else None,
            "received_at": received_at or (datetime.now(timezone.utc) - timedelta(days=400)),
        },
    )
    return message_id


async def _enable_glacier(session: AsyncSession, account_id: uuid.UUID) -> uuid.UUID:
    glacier_folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id) "
            "VALUES (:account_id, true, :glacier_folder_id) "
            "ON CONFLICT (account_id) DO UPDATE SET "
            "glacier_enabled = true, glacier_folder_id = :glacier_folder_id"
        ),
        {"account_id": account_id, "glacier_folder_id": glacier_folder_id},
    )
    return glacier_folder_id


async def _seed_ready_message(
    db: DatabaseConnection, **message_kwargs: object,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """An account with the glacier enabled, one folder, one eligible
    message. Returns (account_id, folder_id, message_id)."""
    async with db.session() as session:
        account_id = await _seed_account(session)
        await _enable_glacier(session, account_id)
        folder_id = await _seed_folder(session, account_id)
        message_id = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, **message_kwargs,  # type: ignore[arg-type]
        )
    return account_id, folder_id, message_id


@pytest.mark.asyncio
async def test_copy_verify_expunge_round_trip(migrated_db: DatabaseConnection) -> None:
    """The ordinary path: a message is copied, verified, and its live row
    is expunged from the mirror -- the mirror-side signal PostIMAP's own
    outbound queue then drains to the real server."""
    account_id, folder_id, message_id = await _seed_ready_message(migrated_db)

    outcome = await copy_message(migrated_db, message_id)
    assert outcome.status == "copied"
    glacier_id = outcome.glacier_id
    assert glacier_id is not None

    verified = await verify_message(migrated_db, glacier_id)
    assert verified is True

    result = await expunge_message(migrated_db, glacier_id)
    assert result == "expunged"

    async with migrated_db.session() as session:
        live = (
            await session.execute(
                text("SELECT expunged_at FROM messages WHERE id = :id"), {"id": message_id},
            )
        ).mappings().one()
        assert live["expunged_at"] is not None

        glacier_row = (
            await session.execute(
                text(
                    "SELECT state, visible_at, raw_source, folder_id FROM glacier_messages "
                    "WHERE id = :id"
                ),
                {"id": glacier_id},
            )
        ).mappings().one()
        # "removing" here -- confirm_or_withdraw_removing (the sweep's own
        # bookkeeping pass) is what promotes it to "glaciered", not this.
        assert glacier_row["state"] == "removing"
        assert glacier_row["visible_at"] is not None
        assert glacier_row["raw_source"] == _RAW_SOURCE
        assert glacier_row["folder_id"] == (
            await session.execute(
                text("SELECT glacier_folder_id FROM account_prefs WHERE account_id = :id"),
                {"id": account_id},
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_corrupted_copy_is_never_verified_or_expunged(
    migrated_db: DatabaseConnection,
) -> None:
    """Section 13.1's first red-team scenario: corrupt the stored copy
    after copy_message and before verify_message. The row must stay
    'copied' and the live message must never be expunged."""
    _, _, message_id = await _seed_ready_message(migrated_db)

    outcome = await copy_message(migrated_db, message_id)
    assert outcome.status == "copied"
    glacier_id = outcome.glacier_id
    assert glacier_id is not None

    async with migrated_db.session() as session:
        await session.execute(
            text(
                "UPDATE glacier_messages SET raw_source = "
                "overlay(raw_source placing 'X' from 1 for 1) WHERE id = :id"
            ),
            {"id": glacier_id},
        )

    verified = await verify_message(migrated_db, glacier_id)
    assert verified is False

    result = await expunge_message(migrated_db, glacier_id)
    assert result == "not_ready"

    async with migrated_db.session() as session:
        row = (
            await session.execute(
                text("SELECT state FROM glacier_messages WHERE id = :id"), {"id": glacier_id},
            )
        ).mappings().one()
        assert row["state"] == "copied"
        live = (
            await session.execute(
                text("SELECT expunged_at FROM messages WHERE id = :id"), {"id": message_id},
            )
        ).mappings().one()
        assert live["expunged_at"] is None


@pytest.mark.asyncio
async def test_expunge_touches_nothing_when_origin_no_longer_matches(
    migrated_db: DatabaseConnection,
) -> None:
    """Section 13.1's second red-team scenario: point origin_message_id
    at a different live message and run the expunge step.
    expunge_if_matches must touch zero rows -- neither message may be
    destroyed."""
    account_id, folder_id, message_id = await _seed_ready_message(migrated_db)

    outcome = await copy_message(migrated_db, message_id)
    assert outcome.status == "copied"
    glacier_id = outcome.glacier_id
    assert glacier_id is not None
    assert await verify_message(migrated_db, glacier_id) is True

    async with migrated_db.session() as session:
        other_message_id = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, imap_uid=2,
        )
        # Simulate a UIDVALIDITY resync racing in: the glacier row now
        # names a message that is not the one it was copied and verified
        # against, even though that id still resolves to a live row.
        await session.execute(
            text("UPDATE glacier_messages SET origin_message_id = :other WHERE id = :gid"),
            {"other": other_message_id, "gid": glacier_id},
        )

    result = await expunge_message(migrated_db, glacier_id)
    assert result == "rolled_back"

    async with migrated_db.session() as session:
        row = (
            await session.execute(
                text("SELECT state FROM glacier_messages WHERE id = :id"), {"id": glacier_id},
            )
        ).mappings().one()
        assert row["state"] == "verified"
        for mid in (message_id, other_message_id):
            live = (
                await session.execute(
                    text("SELECT expunged_at FROM messages WHERE id = :id"), {"id": mid},
                )
            ).mappings().one()
            assert live["expunged_at"] is None, f"{mid} was wrongly expunged"


@pytest.mark.asyncio
async def test_crash_between_copy_and_verify_resumes_cleanly(
    migrated_db: DatabaseConnection,
) -> None:
    """A process dying right after copy_message commits leaves the row
    'copied'; calling verify_message later (the next sweep tick, in
    reality) picks it up and finishes normally -- nothing about resuming
    from a fresh call differs from doing it in the same call."""
    _, _, message_id = await _seed_ready_message(migrated_db)
    outcome = await copy_message(migrated_db, message_id)
    glacier_id = outcome.glacier_id
    assert glacier_id is not None

    # "Crash" here: nothing more happens until a fresh call.
    assert await verify_message(migrated_db, glacier_id) is True
    assert await expunge_message(migrated_db, glacier_id) == "expunged"


@pytest.mark.asyncio
async def test_reresolve_after_uidvalidity_style_id_change(
    migrated_db: DatabaseConnection,
) -> None:
    """Section 3.8: the live row a verified copy points at is gone (its
    id no longer exists -- what a UIDVALIDITY resync does), but a live
    row with the same identity exists under a new id. reresolve_origins
    must find it and repoint origin_message_id, after which expunging
    succeeds against the *new* row."""
    account_id, folder_id, message_id = await _seed_ready_message(
        migrated_db, message_id_hdr="<stable@example.com>",
    )
    outcome = await copy_message(migrated_db, message_id)
    glacier_id = outcome.glacier_id
    assert glacier_id is not None
    assert await verify_message(migrated_db, glacier_id) is True

    async with migrated_db.session() as session:
        # The resync: delete the old row, insert a new one under a fresh
        # id with the same envelope identity (Message-ID, size, date).
        old_row = (
            await session.execute(
                text(
                    "SELECT message_id, size_bytes, received_at FROM messages WHERE id = :id"
                ),
                {"id": message_id},
            )
        ).mappings().one()
        await session.execute(text("DELETE FROM messages WHERE id = :id"), {"id": message_id})
        new_message_id = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, imap_uid=99,
            message_id_hdr=old_row["message_id"], received_at=old_row["received_at"],
        )

    assert await reresolve_origins(migrated_db, account_id) == 1

    async with migrated_db.session() as session:
        row = (
            await session.execute(
                text("SELECT origin_message_id FROM glacier_messages WHERE id = :id"),
                {"id": glacier_id},
            )
        ).mappings().one()
        assert row["origin_message_id"] == new_message_id

    assert await expunge_message(migrated_db, glacier_id) == "expunged"
    async with migrated_db.session() as session:
        live = (
            await session.execute(
                text("SELECT expunged_at FROM messages WHERE id = :id"), {"id": new_message_id},
            )
        ).mappings().one()
        assert live["expunged_at"] is not None


@pytest.mark.asyncio
async def test_truncated_message_is_ineligible(migrated_db: DatabaseConnection) -> None:
    _, _, message_id = await _seed_ready_message(
        migrated_db, raw_source=None, is_truncated=True,
    )
    outcome = await copy_message(migrated_db, message_id)
    assert outcome.status == "ineligible"
    assert "never fully fetched" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_pending_move_is_ineligible(migrated_db: DatabaseConnection) -> None:
    _, _, message_id = await _seed_ready_message(migrated_db, imap_uid=None)
    outcome = await copy_message(migrated_db, message_id)
    assert outcome.status == "ineligible"
    assert "pending" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_disabled_account_is_ineligible(migrated_db: DatabaseConnection) -> None:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session, is_active=False)
        await _enable_glacier(session, account_id)
        folder_id = await _seed_folder(session, account_id)
        message_id = await _seed_message(session, account_id=account_id, folder_id=folder_id)
    outcome = await copy_message(migrated_db, message_id)
    assert outcome.status == "ineligible"
    assert "disabled" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_glacier_not_enabled_is_ineligible(migrated_db: DatabaseConnection) -> None:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        folder_id = await _seed_folder(session, account_id)
        message_id = await _seed_message(session, account_id=account_id, folder_id=folder_id)
    outcome = await copy_message(migrated_db, message_id)
    assert outcome.status == "ineligible"
    assert "not enabled" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_identical_duplicate_is_removable(migrated_db: DatabaseConnection) -> None:
    """Section 3.7: the same message delivered again later, byte for
    byte. The live duplicate may be expunged since a verified copy
    already holds identical bytes."""
    shared_received_at = datetime.now(timezone.utc) - timedelta(days=400)
    account_id, folder_id, message_id = await _seed_ready_message(
        migrated_db, message_id_hdr="<dup@example.com>", received_at=shared_received_at,
    )
    first = await copy_message(migrated_db, message_id)
    assert first.status == "copied"
    assert await verify_message(migrated_db, first.glacier_id) is True
    assert await expunge_message(migrated_db, first.glacier_id) == "expunged"

    async with migrated_db.session() as session:
        # Byte-identical bytes, including the same received_at -- a
        # message delivered again really would carry the same envelope.
        duplicate_id = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, imap_uid=2,
            message_id_hdr="<dup@example.com>", received_at=shared_received_at,
        )

    second = await copy_message(migrated_db, duplicate_id)
    assert second.status == "duplicate_removable"
    assert second.glacier_id == first.glacier_id

    removed = await resolve_duplicate(migrated_db, duplicate_id, second.glacier_id)
    assert removed is True
    async with migrated_db.session() as session:
        live = (
            await session.execute(
                text("SELECT expunged_at FROM messages WHERE id = :id"), {"id": duplicate_id},
            )
        ).mappings().one()
        assert live["expunged_at"] is not None


@pytest.mark.asyncio
async def test_forged_duplicate_with_different_content_is_never_expunged(
    migrated_db: DatabaseConnection,
) -> None:
    """Section 3.7's other branch: a message with the same identity but
    different bytes is never expunged -- two different messages claiming
    one identity is not something to resolve by destroying either."""
    account_id, folder_id, message_id = await _seed_ready_message(
        migrated_db, message_id_hdr="<forged@example.com>",
    )
    first = await copy_message(migrated_db, message_id)
    assert await verify_message(migrated_db, first.glacier_id) is True
    assert await expunge_message(migrated_db, first.glacier_id) == "expunged"

    async with migrated_db.session() as session:
        forged_id = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, imap_uid=3,
            message_id_hdr="<forged@example.com>",
            raw_source=b"From: attacker@example.com\r\nSubject: Forged\r\n\r\nDifferent body\r\n",
        )

    second = await copy_message(migrated_db, forged_id)
    assert second.status == "duplicate_conflict"

    async with migrated_db.session() as session:
        live = (
            await session.execute(
                text("SELECT expunged_at FROM messages WHERE id = :id"), {"id": forged_id},
            )
        ).mappings().one()
        assert live["expunged_at"] is None


@pytest.mark.asyncio
async def test_glacier_message_now_runs_the_whole_sequence(
    migrated_db: DatabaseConnection,
) -> None:
    """The manual move action, end to end. glacier_message_now is gated
    on the running PostIMAP carrying outbox kind="append" (restore must
    work before removal is ever offered -- see
    tests/pg/test_glacier_gate_pg.py), so this needs a capable build to
    exercise the sequence itself; against the pinned default it would
    only re-prove the gate that file already covers exhaustively."""
    async with migrated_db.session() as session:
        info = await read_postimap_info(session)
    if info is None or not supports_message_append(info):
        pytest.skip(
            'this PostIMAP build does not carry outbox kind="append" -- '
            f"reports service_version={info.service_version if info else 'unknown'}, "
            "so glacier_message_now is correctly refused rather than exercised here"
        )
    _, _, message_id = await _seed_ready_message(migrated_db)
    result = await glacier_message_now(migrated_db, message_id)
    assert result.ok is True
    assert result.pending is False
    assert result.glacier_id is not None

    async with migrated_db.session() as session:
        row = (
            await session.execute(
                text("SELECT state FROM glacier_messages WHERE id = :id"),
                {"id": result.glacier_id},
            )
        ).mappings().one()
        assert row["state"] == "removing"
        live = (
            await session.execute(
                text("SELECT expunged_at FROM messages WHERE id = :id"), {"id": message_id},
            )
        ).mappings().one()
        assert live["expunged_at"] is not None


@pytest.mark.asyncio
async def test_eligibility_reason_names_the_failing_guard(migrated_db: DatabaseConnection) -> None:
    _, _, message_id = await _seed_ready_message(migrated_db, is_draft=True)
    async with migrated_db.session() as session:
        reason = await eligibility_reason(session, message_id)
    assert reason == "drafts cannot be glaciered"


@pytest.mark.asyncio
async def test_attachments_and_content_hash_survive_the_copy(
    migrated_db: DatabaseConnection,
) -> None:
    account_id, folder_id, message_id = await _seed_ready_message(migrated_db)
    attachment_data = b"PDF bytes here"
    async with migrated_db.session() as session:
        await session.execute(
            text(
                "INSERT INTO attachments (id, message_id, filename, content_type, size_bytes, "
                "data) VALUES (:id, :message_id, 'file.pdf', 'application/pdf', :size, :data)"
            ),
            {
                "id": uuid.uuid4(), "message_id": message_id, "size": len(attachment_data),
                "data": attachment_data,
            },
        )

    outcome = await copy_message(migrated_db, message_id)
    assert outcome.status == "copied"
    glacier_id = outcome.glacier_id
    assert glacier_id is not None
    assert await verify_message(migrated_db, glacier_id) is True

    async with migrated_db.session() as session:
        glacier_row = (
            await session.execute(
                text(
                    "SELECT attachment_count, content_sha256 FROM glacier_messages WHERE id = :id"
                ),
                {"id": glacier_id},
            )
        ).mappings().one()
        assert glacier_row["attachment_count"] == 1
        assert glacier_row["content_sha256"] == hashlib.sha256(_RAW_SOURCE).digest()
        att = (
            await session.execute(
                text(
                    "SELECT data, filename FROM glacier_attachments WHERE glacier_message_id = :id"
                ),
                {"id": glacier_id},
            )
        ).mappings().one()
        assert att["data"] == attachment_data
        assert att["filename"] == "file.pdf"


async def _insert_delete_notification(
    session: AsyncSession, *, account_id: uuid.UUID, origin_message_id: uuid.UUID,
    created_at: datetime, acknowledged: bool, reverted: bool,
) -> None:
    await session.execute(
        text(
            "INSERT INTO sync_notifications "
            "(account_id, action, message_id, error, acknowledged_at, reverted_at, created_at) "
            "VALUES (:account_id, 'delete', :origin_id, 'NO [CANNOT] deleted flag rejected', "
            ":acknowledged_at, :reverted_at, :created_at)"
        ),
        {
            "account_id": account_id, "origin_id": origin_message_id,
            "acknowledged_at": datetime.now(timezone.utc) if acknowledged else None,
            "reverted_at": datetime.now(timezone.utc) if reverted else None,
            "created_at": created_at,
        },
    )


@pytest.mark.asyncio
async def test_stale_acknowledged_notification_does_not_withdraw_a_successful_expunge(
    migrated_db: DatabaseConnection,
) -> None:
    """The red team's permanent-loss reproduction: an old, acknowledged,
    already-reverted delete notification for this message -- left over
    from an unrelated permanent-delete attempt that failed long before
    this message was ever glaciered -- must not be mistaken for evidence
    that *this* expunge failed. Its own timestamp predates
    expunge_requested_at, so it names a different attempt entirely."""
    account_id, _, message_id = await _seed_ready_message(migrated_db)

    outcome = await copy_message(migrated_db, message_id)
    glacier_id = outcome.glacier_id
    assert glacier_id is not None
    assert await verify_message(migrated_db, glacier_id) is True
    assert await expunge_message(migrated_db, glacier_id) == "expunged"

    async with migrated_db.session() as session:
        await _insert_delete_notification(
            session, account_id=account_id, origin_message_id=message_id,
            created_at=datetime.now(timezone.utc) - timedelta(days=30),
            acknowledged=True, reverted=True,
        )

    confirmed, withdrawn = await confirm_or_withdraw_removing(
        migrated_db, account_id, grace_seconds=0,
    )
    assert confirmed == 1
    assert withdrawn == 0

    async with migrated_db.session() as session:
        row = (
            await session.execute(
                text("SELECT state, raw_source FROM glacier_messages WHERE id = :id"),
                {"id": glacier_id},
            )
        ).mappings().one()
        assert row["state"] == "glaciered"
        assert row["raw_source"] == _RAW_SOURCE
        att_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM glacier_attachments WHERE glacier_message_id = :id"
                ),
                {"id": glacier_id},
            )
        ).scalar_one()
        # No attachments were seeded on this message, but the withdraw
        # path (wrongly) deletes this row outright when it fires -- so
        # confirming the row is untouched is the real assertion here,
        # this count is just belt-and-braces.
        assert att_count == 0


@pytest.mark.asyncio
async def test_fresh_reverted_notification_withdraws_only_once_live_row_is_intact(
    migrated_db: DatabaseConnection,
) -> None:
    """The other side of section 3.6: a *fresh* delete-failure
    notification (after expunge_requested_at) whose revert has actually
    landed (reverted_at set) means the live message is authoritative
    again -- withdrawing the glacier copy is correct once, and only
    once, the live row is verifiably back (not expunged, still on the
    server)."""
    account_id, _, message_id = await _seed_ready_message(migrated_db)

    outcome = await copy_message(migrated_db, message_id)
    glacier_id = outcome.glacier_id
    assert glacier_id is not None
    assert await verify_message(migrated_db, glacier_id) is True
    assert await expunge_message(migrated_db, glacier_id) == "expunged"

    async with migrated_db.session() as session:
        # Simulate PostIMAP's own revert: expunged_at cleared, the
        # message intact on the server under its original imap_uid.
        await session.execute(
            text("UPDATE messages SET expunged_at = NULL WHERE id = :id"), {"id": message_id},
        )
        await _insert_delete_notification(
            session, account_id=account_id, origin_message_id=message_id,
            created_at=datetime.now(timezone.utc), acknowledged=False, reverted=True,
        )

    confirmed, withdrawn = await confirm_or_withdraw_removing(
        migrated_db, account_id, grace_seconds=0,
    )
    assert confirmed == 0
    assert withdrawn == 1

    async with migrated_db.session() as session:
        glacier_row = (
            await session.execute(
                text("SELECT id FROM glacier_messages WHERE id = :id"), {"id": glacier_id},
            )
        ).mappings().one_or_none()
        assert glacier_row is None
        live = (
            await session.execute(
                text("SELECT expunged_at, imap_uid FROM messages WHERE id = :id"),
                {"id": message_id},
            )
        ).mappings().one()
        assert live["expunged_at"] is None
        assert live["imap_uid"] is not None


@pytest.mark.asyncio
async def test_fresh_notification_without_a_landed_revert_is_left_pending(
    migrated_db: DatabaseConnection,
) -> None:
    """Reasoned by the red team, not reproduced: a fresh delete-failure
    notification whose revert has not landed yet (reverted_at NULL) is
    not evidence of anything either way -- the live row's own column may
    still hold whatever this expunge wrote. The only copy that could
    still exist anywhere must not be deleted on a guess; the row stays
    in "removing" for a later tick to resolve."""
    account_id, _, message_id = await _seed_ready_message(migrated_db)

    outcome = await copy_message(migrated_db, message_id)
    glacier_id = outcome.glacier_id
    assert glacier_id is not None
    assert await verify_message(migrated_db, glacier_id) is True
    assert await expunge_message(migrated_db, glacier_id) == "expunged"

    async with migrated_db.session() as session:
        await _insert_delete_notification(
            session, account_id=account_id, origin_message_id=message_id,
            created_at=datetime.now(timezone.utc), acknowledged=False, reverted=False,
        )

    confirmed, withdrawn = await confirm_or_withdraw_removing(
        migrated_db, account_id, grace_seconds=0,
    )
    assert confirmed == 0
    assert withdrawn == 0

    async with migrated_db.session() as session:
        row = (
            await session.execute(
                text("SELECT state, raw_source FROM glacier_messages WHERE id = :id"),
                {"id": glacier_id},
            )
        ).mappings().one()
        assert row["state"] == "removing"
        assert row["raw_source"] == _RAW_SOURCE


@pytest.mark.asyncio
async def test_glacier_message_now_names_a_duplicate_it_resolved(
    migrated_db: DatabaseConnection,
) -> None:
    """The design's own requirement, missed the first time: glaciering a
    live duplicate of an already-glaciered message removes the server's
    copy silently unless the outcome says so. success stays true (the
    server copy really was removed, correctly), but the reason must name
    what happened rather than being None."""
    async with migrated_db.session() as session:
        info = await read_postimap_info(session)
    if info is None or not supports_message_append(info):
        pytest.skip(
            'this PostIMAP build does not carry outbox kind="append" -- '
            f"reports service_version={info.service_version if info else 'unknown'}"
        )
    shared_received_at = datetime.now(timezone.utc) - timedelta(days=400)
    account_id, folder_id, message_id = await _seed_ready_message(
        migrated_db, message_id_hdr="<dup-now@example.com>", received_at=shared_received_at,
    )
    first = await glacier_message_now(migrated_db, message_id)
    assert first.ok, first.reason

    async with migrated_db.session() as session:
        duplicate_id = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, imap_uid=2,
            message_id_hdr="<dup-now@example.com>", received_at=shared_received_at,
        )

    second = await glacier_message_now(migrated_db, duplicate_id)
    assert second.ok is True
    assert second.glacier_id == first.glacier_id
    assert second.reason is not None
    assert "duplicate" in second.reason

    async with migrated_db.session() as session:
        live = (
            await session.execute(
                text("SELECT expunged_at FROM messages WHERE id = :id"), {"id": duplicate_id},
            )
        ).mappings().one()
        assert live["expunged_at"] is not None
