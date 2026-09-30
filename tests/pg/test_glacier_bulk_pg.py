"""
Bulk-moving several messages into the glacier at once, through the same
POST .../bulk-action a "select all, archive" or "empty this folder"
flow in the UI drives.

Skips itself against the pinned default PostIMAP image the same way
tests/pg/test_glacier_api_pg.py does -- see that file's own docstring.
"""

from __future__ import annotations

import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import mail_verdict.api.mails as mails_module
from mail_verdict.api.mails import bulk_action as api_bulk_action
from mail_verdict.api.schemas import BulkActionRequest, BulkActionScope
from mail_verdict.config.loader import GlacierConfig
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


async def _seed_glacier_messages(
    session: AsyncSession, *, account_id: uuid.UUID, glacier_folder_id: uuid.UUID, count: int,
) -> list[uuid.UUID]:
    """`count` visible, terminal ("glaciered") rows, ready to be marked,
    restored or expunged -- hand-seeded rather than run through the real
    glacier_message_now flow, since none of this needs the append
    capability the way restoring one back out does."""
    ids = [uuid.uuid4() for _ in range(count)]
    now = datetime.now(timezone.utc)
    for glacier_id in ids:
        await session.execute(
            text(
                "INSERT INTO glacier_messages "
                "(id, account_id, folder_id, thread_id, message_id, subject, from_addr, "
                " to_addrs, body_text, raw_source, size_bytes, received_at, is_seen, "
                " msg_key, state, visible_at, glaciered_at) "
                "VALUES (:id, :account_id, :folder_id, :thread_id, :message_id_hdr, "
                " 'Bulk glacier test', 'sender@example.com', '[\"me@example.com\"]', "
                " 'Body', :raw_source, :size_bytes, :received_at, false, "
                " :msg_key, 'glaciered', :visible_at, :visible_at)"
            ),
            {
                "id": glacier_id, "account_id": account_id, "folder_id": glacier_folder_id,
                "thread_id": glacier_id, "message_id_hdr": f"<{glacier_id}@example.com>",
                "raw_source": _RAW_SOURCE, "size_bytes": len(_RAW_SOURCE), "received_at": now,
                "msg_key": f"msg-{glacier_id}", "visible_at": now,
            },
        )
    return ids


@pytest.mark.asyncio
async def test_bulk_mark_read_by_explicit_ids(migrated_db: DatabaseConnection) -> None:
    async with migrated_db.session() as session:
        account_id, _folder_id, glacier_folder_id, _live_ids = (
            await _seed_account_with_messages(session, count=0)
        )
        glacier_ids = await _seed_glacier_messages(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id, count=3,
        )
        await session.commit()

    response = await api_bulk_action(
        account_id, BulkActionRequest(action="mark_read", ids=glacier_ids),
    )

    assert response.success is True, response.errors
    assert response.affected_count == 3
    assert response.skipped_ids == []

    async with migrated_db.session() as session:
        unread = (
            await session.execute(
                text(
                    "SELECT count(*) FROM glacier_messages "
                    "WHERE id = ANY(:ids) AND is_seen = false"
                ),
                {"ids": glacier_ids},
            )
        ).scalar_one()
        assert unread == 0


@pytest.mark.asyncio
async def test_bulk_mark_read_by_the_glacier_as_scope(migrated_db: DatabaseConnection) -> None:
    async with migrated_db.session() as session:
        account_id, _folder_id, glacier_folder_id, _live_ids = (
            await _seed_account_with_messages(session, count=0)
        )
        glacier_ids = await _seed_glacier_messages(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id, count=2,
        )
        await session.commit()

    response = await api_bulk_action(
        account_id,
        BulkActionRequest(
            action="mark_read",
            scope=BulkActionScope(
                folder_id=glacier_folder_id, snapshot_at=datetime.now(timezone.utc),
            ),
        ),
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
                {"ids": glacier_ids},
            )
        ).scalar_one()
        assert unread == 0


@pytest.mark.asyncio
async def test_bulk_expunge_requires_confirmation_then_deletes(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        account_id, _folder_id, glacier_folder_id, _live_ids = (
            await _seed_account_with_messages(session, count=0)
        )
        glacier_ids = await _seed_glacier_messages(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id, count=2,
        )
        await session.commit()

    # Without confirm: refused, and nothing was written -- the whole
    # point of the guard is that a caller cannot get a silent success
    # while the messages are still there.
    unconfirmed = await api_bulk_action(
        account_id, BulkActionRequest(action="expunge", ids=glacier_ids),
    )
    assert unconfirmed.success is False
    assert unconfirmed.affected_count == 0

    async with migrated_db.session() as session:
        still_there = (
            await session.execute(
                text("SELECT count(*) FROM glacier_messages WHERE id = ANY(:ids)"),
                {"ids": glacier_ids},
            )
        ).scalar_one()
        assert still_there == 2

    confirmed = await api_bulk_action(
        account_id, BulkActionRequest(action="expunge", ids=glacier_ids, confirm=True),
    )
    assert confirmed.success is True, confirmed.errors
    assert confirmed.affected_count == 2

    async with migrated_db.session() as session:
        remaining = (
            await session.execute(
                text("SELECT count(*) FROM glacier_messages WHERE id = ANY(:ids)"),
                {"ids": glacier_ids},
            )
        ).scalar_one()
        assert remaining == 0


@pytest.mark.asyncio
async def test_bulk_restore_to_a_server_folder(migrated_db: DatabaseConnection) -> None:
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        account_id, folder_id, glacier_folder_id, _live_ids = (
            await _seed_account_with_messages(session, count=0)
        )
        glacier_ids = await _seed_glacier_messages(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id, count=2,
        )
        await session.commit()

    response = await api_bulk_action(
        account_id,
        BulkActionRequest(action="move", target_folder_id=folder_id, ids=glacier_ids),
    )

    assert response.success is True, response.errors
    assert response.affected_count == 2

    async with migrated_db.session() as session:
        restoring = (
            await session.execute(
                text(
                    "SELECT count(*) FROM glacier_messages "
                    "WHERE id = ANY(:ids) AND state = 'restoring'"
                ),
                {"ids": glacier_ids},
            )
        ).scalar_one()
        assert restoring == 2


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


def _with_manual_batch_cap(monkeypatch: pytest.MonkeyPatch, cap: int) -> None:
    """`glacier.max_manual_batch` (config/loader.py's GlacierConfig) is
    declared and never read anywhere -- the red team's finding. Patched
    at the point _refuse_over_manual_batch_cap actually reads it, rather
    than through config.yaml, since every other pg test already runs
    against the repo's real config and this cap needs to be small enough
    to trip with a handful of seeded messages."""
    fake_config = types.SimpleNamespace(
        glacier=GlacierConfig(
            sweep_enabled=True, interval_seconds=60, batch_size=25, max_unconfirmed=500,
            confirm_grace_seconds=600, max_manual_batch=cap, restore_timeout_seconds=1800,
        ),
    )
    monkeypatch.setattr(mails_module, "get_config", lambda: fake_config)


@pytest.mark.asyncio
async def test_bulk_move_into_glacier_over_the_cap_is_refused(
    migrated_db: DatabaseConnection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The red team's finding: glacier.max_manual_batch is declared and
    read nowhere -- a bulk move of any size was accepted, which for a
    real archive would queue thousands of IMAP EXPUNGEs into PostIMAP's
    outbound queue at once."""
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        account_id, _folder_id, glacier_folder_id, message_ids = (
            await _seed_account_with_messages(session, count=3)
        )
        await session.commit()
    _with_manual_batch_cap(monkeypatch, cap=2)

    with pytest.raises(Exception) as exc_info:  # noqa: PT011
        await api_bulk_action(
            account_id,
            BulkActionRequest(action="move", target_folder_id=glacier_folder_id, ids=message_ids),
        )
    message = str(exc_info.value)
    assert "2" in message
    assert "3" in message

    async with migrated_db.session() as session:
        live = (
            await session.execute(
                text("SELECT count(*) FROM messages WHERE id = ANY(:ids) AND expunged_at IS NULL"),
                {"ids": message_ids},
            )
        ).scalar_one()
        assert live == 3


@pytest.mark.asyncio
async def test_bulk_restore_over_the_cap_is_refused(
    migrated_db: DatabaseConnection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The uncapped-restore side of the same finding: 'restore is
    uncapped for the same reason' -- a bulk restore over the cap must be
    refused the same way a bulk move-in is, before any APPEND is queued."""
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        account_id, folder_id, glacier_folder_id, _live_ids = (
            await _seed_account_with_messages(session, count=0)
        )
        glacier_ids = await _seed_glacier_messages(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id, count=3,
        )
        await session.commit()
    _with_manual_batch_cap(monkeypatch, cap=2)

    with pytest.raises(Exception) as exc_info:  # noqa: PT011
        await api_bulk_action(
            account_id,
            BulkActionRequest(action="move", target_folder_id=folder_id, ids=glacier_ids),
        )
    message = str(exc_info.value)
    assert "2" in message
    assert "3" in message

    async with migrated_db.session() as session:
        still_glaciered = (
            await session.execute(
                text(
                    "SELECT count(*) FROM glacier_messages "
                    "WHERE id = ANY(:ids) AND state = 'glaciered'"
                ),
                {"ids": glacier_ids},
            )
        ).scalar_one()
        assert still_glaciered == 3
