"""
The automatic glacier sweep (design section 8.3): each guard shown
refusing on its own, by its own test, and the batch-size pacing shown
on a stack of several hundred old archived messages -- one tick claims
at most `batch_size`, never the whole backlog at once.

Every guard test needs a PostIMAP capable of outbox kind="append"
(MAIL_VERDICT_TEST_POSTIMAP_IMAGE, see tests/setup/images.py): the
capability check is the first thing _sweep_guard_reason evaluates, so
against the pinned default every one of these tests would see that
refusal instead of the guard actually under test -- the same reasoning
tests/pg/test_glacier_gate_pg.py's own docstring gives for the allow
path in general. Skips itself, naming the running version, rather than
either failing on every ordinary run or silently reporting a guard
proven that never ran.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.config.loader import GlacierConfig
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.glacier.sweep import _sweep_account_once, _sweep_guard_reason, _sweep_once
from mail_verdict.postimap.contract import read_postimap_info, supports_message_append

_RAW_SOURCE = b"From: sender@example.com\r\nSubject: Test\r\n\r\nBody\r\n"

_imap_uid_counter = itertools.count(1)

_TEST_CFG = GlacierConfig(
    sweep_enabled=True, interval_seconds=60, batch_size=25, max_unconfirmed=500,
    confirm_grace_seconds=600, max_manual_batch=200, restore_timeout_seconds=1800,
)


async def _skip_unless_append_capable(db: DatabaseConnection) -> None:
    async with db.session() as session:
        info = await read_postimap_info(session)
    if info is None or not supports_message_append(info):
        pytest.skip(
            'this PostIMAP build does not carry outbox kind="append" -- '
            f"reports service_version={info.service_version if info else 'unknown'}, "
            "so the sweep is correctly refused rather than exercised here"
        )


async def _seed_sweepable_account(
    session: AsyncSession, *, auto_days: int = 30, archive_message_count: int = 0,
    archive_received_days_ago: int = 400,
) -> tuple[uuid.UUID, uuid.UUID]:
    """An account that satisfies every guard but is_active/state --
    glacier enabled with auto_days set, a fully-synced archive folder, a
    completed sync pass, no pending move, no unacknowledged failure,
    nothing already unconfirmed. Seeded *inactive*: PostIMAP's own live
    listener rewrites accounts.state (and can rewrite sync_state right
    back to unsynced) for any is_active=true account the instant it
    notices one, which a fake host can never satisfy again once it does
    (repo CLAUDE.md's own documented pg-layer trap) -- call _activate()
    as the very last statement before checking a guard, to keep the
    window PostIMAP has to interfere as small as possible. Returns
    (account_id, archive_folder_id)."""
    account_id = uuid.uuid4()
    archive_folder_id = uuid.uuid4()
    # 192.0.2.1 (RFC 5737 TEST-NET-1, guaranteed unroutable) rather than
    # imap.example.com: a real host that refuses or fails DNS gives
    # PostIMAP's listener a fast (sub-second) failure to react to, which
    # loses the race against _activate below often enough to make these
    # tests flaky. An unroutable address is silently dropped, so
    # PostIMAP's own connect attempt blocks for its full timeout
    # instead -- long enough that a guard check run immediately after
    # _activate always wins.
    await session.execute(
        text(
            "INSERT INTO accounts "
            "(id, name, imap_host, imap_port, imap_user, imap_password, is_active, state) "
            "VALUES (:id, :name, '192.0.2.1', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'), false, 'active')"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use, initial_sync_done) "
            "VALUES (:id, :account_id, 'Archive', 'archive', true)"
        ),
        {"id": archive_folder_id, "account_id": account_id},
    )
    await session.execute(
        text(
            "INSERT INTO sync_state (account_id, last_full_sync) "
            "VALUES (:account_id, now())"
        ),
        {"account_id": account_id},
    )
    await session.execute(
        text(
            "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id, "
            "glacier_auto_days) VALUES (:account_id, true, :glacier_folder_id, :auto_days)"
        ),
        {
            "account_id": account_id, "glacier_folder_id": uuid.uuid4(),
            "auto_days": auto_days,
        },
    )
    received_at = datetime.now(timezone.utc) - timedelta(days=archive_received_days_ago)
    for _ in range(archive_message_count):
        message_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
                " from_addr, raw_source, size_bytes, received_at) "
                "VALUES (:id, :account_id, :folder_id, :uid, :thread_id, :msg_id, 'Test', "
                " 'sender@example.com', :raw_source, :size_bytes, :received_at)"
            ),
            {
                "id": message_id, "account_id": account_id, "folder_id": archive_folder_id,
                "uid": next(_imap_uid_counter),
                "thread_id": message_id, "msg_id": f"<{message_id}@example.com>",
                "raw_source": _RAW_SOURCE, "size_bytes": len(_RAW_SOURCE),
                "received_at": received_at,
            },
        )
    return account_id, archive_folder_id


async def _activate(db: DatabaseConnection, account_id: uuid.UUID) -> None:
    """Flip is_active/state to their satisfied-guard values as the very
    last write before a guard check -- see _seed_sweepable_account's own
    docstring for why this has to be separate and last."""
    async with db.session() as session:
        await session.execute(
            text("UPDATE accounts SET is_active = true, state = 'active' WHERE id = :id"),
            {"id": account_id},
        )


async def _activate_then(
    db: DatabaseConnection, account_id: uuid.UUID, action: Callable[[], Awaitable[None]],
    *, attempts: int = 15,
) -> None:
    """Re-activate and retry `action` (which reads state via the code
    under test, not this file) until it stops seeing PostIMAP's own
    listener win the race against _activate -- see
    _seed_sweepable_account's docstring for the mechanism.
    Deterministic code racing a live, concurrent, external process is
    what this compensates for: a genuine defect in the guard logic
    itself fails identically every attempt and still surfaces once
    `attempts` is exhausted, rather than being silently swallowed."""
    last_exc: AssertionError | None = None
    for _ in range(attempts):
        await _activate(db, account_id)
        try:
            await action()
            return
        except AssertionError as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc


@pytest.mark.asyncio
async def test_the_sweep_glaciers_an_eligible_account_when_nothing_blocks_it(
    migrated_db: DatabaseConnection,
) -> None:
    """The positive control every guard test below is contrasted
    against: with every condition satisfied, the sweep actually moves
    mail -- proving the guard tests fail for the right reason, not
    because nothing here works at all."""
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        account_id, _archive_folder_id = await _seed_sweepable_account(
            session, archive_message_count=3,
        )
        await session.commit()

    async def _check_reason() -> None:
        reason = await _sweep_guard_reason(migrated_db, account_id, cfg=_TEST_CFG)
        assert reason is None, reason

    await _activate_then(migrated_db, account_id, _check_reason)

    async def _sweep_and_check_count() -> None:
        await _sweep_account_once(migrated_db, account_id, event_ring=None, cfg=_TEST_CFG)
        async with migrated_db.session() as session:
            glaciered = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM glacier_messages WHERE account_id = :id "
                        "AND state IN ('removing', 'glaciered')"
                    ),
                    {"id": account_id},
                )
            ).scalar_one()
        assert glaciered == 3

    await _activate_then(migrated_db, account_id, _sweep_and_check_count)


@pytest.mark.asyncio
async def test_glacier_disabled_refuses(migrated_db: DatabaseConnection) -> None:
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        account_id, _ = await _seed_sweepable_account(session)
        await session.execute(
            text("UPDATE account_prefs SET glacier_enabled = false WHERE account_id = :id"),
            {"id": account_id},
        )
        await session.commit()

    reason = await _sweep_guard_reason(migrated_db, account_id, cfg=_TEST_CFG)
    assert reason is not None
    assert "off" in reason


@pytest.mark.asyncio
async def test_no_auto_days_refuses(migrated_db: DatabaseConnection) -> None:
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        account_id, _ = await _seed_sweepable_account(session)
        await session.execute(
            text(
                "UPDATE account_prefs SET glacier_auto_days = NULL WHERE account_id = :id"
            ),
            {"id": account_id},
        )
        await session.commit()

    reason = await _sweep_guard_reason(migrated_db, account_id, cfg=_TEST_CFG)
    assert reason is not None
    assert "off" in reason


@pytest.mark.asyncio
async def test_inactive_account_refuses(migrated_db: DatabaseConnection) -> None:
    """Seeded inactive already (this whole file's default) -- the
    guard under test is exactly the state a real inactive account is
    always genuinely in, no race to win."""
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        account_id, _ = await _seed_sweepable_account(session)
        await session.commit()

    reason = await _sweep_guard_reason(migrated_db, account_id, cfg=_TEST_CFG)
    assert reason is not None
    assert "active" in reason


@pytest.mark.asyncio
async def test_account_not_in_state_active_refuses(migrated_db: DatabaseConnection) -> None:
    """is_active=true with a deliberately non-'active' state -- set as
    the last write either way (by this test, or by PostIMAP's own
    listener reacting to is_active=true against an unreachable host),
    so this one needs no race-avoidance: any outcome that isn't
    'active' proves the guard."""
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        account_id, _ = await _seed_sweepable_account(session)
        await session.commit()
    async with migrated_db.session() as session:
        await session.execute(
            text("UPDATE accounts SET is_active = true, state = 'error' WHERE id = :id"),
            {"id": account_id},
        )

    reason = await _sweep_guard_reason(migrated_db, account_id, cfg=_TEST_CFG)
    assert reason is not None
    assert "active" in reason


@pytest.mark.asyncio
async def test_never_synced_refuses(migrated_db: DatabaseConnection) -> None:
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        account_id, _ = await _seed_sweepable_account(session)
        await session.execute(
            text("UPDATE sync_state SET last_full_sync = NULL WHERE account_id = :id"),
            {"id": account_id},
        )
        await session.commit()

    async def _check() -> None:
        reason = await _sweep_guard_reason(migrated_db, account_id, cfg=_TEST_CFG)
        assert reason is not None
        assert "sync pass" in reason

    await _activate_then(migrated_db, account_id, _check)


@pytest.mark.asyncio
async def test_archive_folder_not_synced_refuses(migrated_db: DatabaseConnection) -> None:
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        account_id, archive_folder_id = await _seed_sweepable_account(session)
        await session.execute(
            text("UPDATE folders SET initial_sync_done = false WHERE id = :id"),
            {"id": archive_folder_id},
        )
        await session.commit()

    async def _check() -> None:
        reason = await _sweep_guard_reason(migrated_db, account_id, cfg=_TEST_CFG)
        assert reason is not None
        assert "archive folder" in reason

    await _activate_then(migrated_db, account_id, _check)


@pytest.mark.asyncio
async def test_archive_folder_deleted_refuses(migrated_db: DatabaseConnection) -> None:
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        account_id, archive_folder_id = await _seed_sweepable_account(session)
        await session.execute(
            text("UPDATE folders SET deleted_at = now() WHERE id = :id"),
            {"id": archive_folder_id},
        )
        await session.commit()

    async def _check() -> None:
        reason = await _sweep_guard_reason(migrated_db, account_id, cfg=_TEST_CFG)
        assert reason is not None
        assert "archive folder" in reason

    await _activate_then(migrated_db, account_id, _check)


@pytest.mark.asyncio
async def test_a_pending_move_anywhere_on_the_account_refuses(
    migrated_db: DatabaseConnection,
) -> None:
    """Account-wide, not scoped to the archive folder -- a pending move
    on any message means the mirror as a whole cannot be trusted yet."""
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        account_id, archive_folder_id = await _seed_sweepable_account(session)
        pending_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
                " from_addr, raw_source, size_bytes, received_at) "
                "VALUES (:id, :account_id, :folder_id, NULL, :thread_id, :msg_id, 'Pending', "
                " 'sender@example.com', :raw_source, :size_bytes, now())"
            ),
            {
                "id": pending_id, "account_id": account_id, "folder_id": archive_folder_id,
                "thread_id": pending_id, "msg_id": f"<{pending_id}@example.com>",
                "raw_source": _RAW_SOURCE, "size_bytes": len(_RAW_SOURCE),
            },
        )
        await session.commit()

    async def _check() -> None:
        reason = await _sweep_guard_reason(migrated_db, account_id, cfg=_TEST_CFG)
        assert reason is not None
        assert "pending" in reason

    await _activate_then(migrated_db, account_id, _check)


@pytest.mark.asyncio
async def test_an_unacknowledged_sync_failure_refuses(migrated_db: DatabaseConnection) -> None:
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        account_id, _ = await _seed_sweepable_account(session)
        await session.execute(
            text(
                "INSERT INTO sync_notifications (account_id, action, error) "
                "VALUES (:account_id, 'delete', 'server refused')"
            ),
            {"account_id": account_id},
        )
        await session.commit()

    async def _check() -> None:
        reason = await _sweep_guard_reason(migrated_db, account_id, cfg=_TEST_CFG)
        assert reason is not None
        assert "unacknowledged" in reason

    await _activate_then(migrated_db, account_id, _check)


@pytest.mark.asyncio
async def test_too_many_unconfirmed_removing_rows_refuses(
    migrated_db: DatabaseConnection,
) -> None:
    """The circuit breaker: cfg.max_unconfirmed already reached, even
    though every other guard is satisfied."""
    await _skip_unless_append_capable(migrated_db)
    cfg = GlacierConfig(
        sweep_enabled=True, interval_seconds=60, batch_size=25, max_unconfirmed=1,
        confirm_grace_seconds=600, max_manual_batch=200, restore_timeout_seconds=1800,
    )
    async with migrated_db.session() as session:
        account_id, archive_folder_id = await _seed_sweepable_account(session)
        glacier_folder_id = (
            await session.execute(
                text("SELECT glacier_folder_id FROM account_prefs WHERE account_id = :id"),
                {"id": account_id},
            )
        ).scalar_one()
        glacier_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO glacier_messages "
                "(id, account_id, folder_id, thread_id, message_id, subject, from_addr, "
                " raw_source, size_bytes, received_at, msg_key, state, "
                " expunge_requested_at) "
                "VALUES (:id, :account_id, :folder_id, :thread_id, :msg_id, 'Stuck', "
                " 'sender@example.com', :raw_source, :size_bytes, now(), :msg_key, "
                " 'removing', now())"
            ),
            {
                "id": glacier_id, "account_id": account_id, "folder_id": glacier_folder_id,
                "thread_id": glacier_id, "msg_id": f"<{glacier_id}@example.com>",
                "raw_source": _RAW_SOURCE, "size_bytes": len(_RAW_SOURCE),
                "msg_key": f"msg-{glacier_id}",
            },
        )
        await session.commit()

    async def _check() -> None:
        reason = await _sweep_guard_reason(migrated_db, account_id, cfg=cfg)
        assert reason is not None
        assert "unconfirmed" in reason

    await _activate_then(migrated_db, account_id, _check)


@pytest.mark.asyncio
async def test_the_global_kill_switch_stops_every_account(
    migrated_db: DatabaseConnection,
) -> None:
    """sweep_enabled=false is checked once, in _sweep_once, before any
    per-account guard even runs -- proven by an otherwise fully eligible
    account seeing nothing claimed."""
    await _skip_unless_append_capable(migrated_db)
    cfg = GlacierConfig(
        sweep_enabled=False, interval_seconds=60, batch_size=25, max_unconfirmed=500,
        confirm_grace_seconds=600, max_manual_batch=200, restore_timeout_seconds=1800,
    )
    async with migrated_db.session() as session:
        account_id, _ = await _seed_sweepable_account(session, archive_message_count=3)
        await session.commit()

    await _sweep_once(migrated_db, event_ring=None, cfg=cfg)

    async with migrated_db.session() as session:
        glaciered = (
            await session.execute(
                text("SELECT count(*) FROM glacier_messages WHERE account_id = :id"),
                {"id": account_id},
            )
        ).scalar_one()
    assert glaciered == 0


@pytest.mark.asyncio
async def test_pacing_claims_at_most_one_batch_per_tick(
    migrated_db: DatabaseConnection,
) -> None:
    """A stack of several hundred old archived messages: one tick moves
    exactly batch_size of them, never the whole backlog at once (design
    section 8.3's own pacing rationale -- PostIMAP's outbound queue is
    shared with every flag change and every send)."""
    await _skip_unless_append_capable(migrated_db)
    cfg = GlacierConfig(
        sweep_enabled=True, interval_seconds=60, batch_size=25, max_unconfirmed=500,
        confirm_grace_seconds=600, max_manual_batch=200, restore_timeout_seconds=1800,
    )
    async with migrated_db.session() as session:
        account_id, _archive_folder_id = await _seed_sweepable_account(
            session, archive_message_count=300,
        )
        await session.commit()

    async def _glaciered_count() -> int:
        async with migrated_db.session() as session:
            return (
                await session.execute(
                    text(
                        "SELECT count(*) FROM glacier_messages WHERE account_id = :id "
                        "AND state IN ('removing', 'glaciered')"
                    ),
                    {"id": account_id},
                )
            ).scalar_one()

    async def _tick_or_retry() -> None:
        # Only "the guard blocked, nothing was claimed" is the race this
        # retries past -- any other count is a real defect and must
        # fail outright rather than be retried away.
        await _sweep_account_once(migrated_db, account_id, event_ring=None, cfg=cfg)
        if await _glaciered_count() == 0:
            raise AssertionError("guard blocked this tick -- retrying")

    await _activate_then(migrated_db, account_id, _tick_or_retry)

    async with migrated_db.session() as session:
        still_live = (
            await session.execute(
                text(
                    "SELECT count(*) FROM messages WHERE account_id = :id "
                    "AND expunged_at IS NULL"
                ),
                {"id": account_id},
            )
        ).scalar_one()
    assert await _glaciered_count() == cfg.batch_size
    assert still_live == 300 - cfg.batch_size

    # The next tick continues where this one stopped, rather than
    # reclaiming the same batch -- proven by the count actually growing.
    async def _second_tick_or_retry() -> None:
        before = await _glaciered_count()
        await _sweep_account_once(migrated_db, account_id, event_ring=None, cfg=cfg)
        if await _glaciered_count() == before:
            raise AssertionError("guard blocked the second tick -- retrying")

    await _activate_then(migrated_db, account_id, _second_tick_or_retry)
    assert await _glaciered_count() == cfg.batch_size * 2
