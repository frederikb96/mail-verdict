"""
The automatic glacier sweep: an account with glacier_auto_days set gets
its own archive moved into the glacier, in small paced batches, on a
schedule -- section 8.3 of this feature's design.

Every tick, per account with a glacier at all (enabled or not -- a
disabled glacier can still hold mid-flight rows that must be seen
through, see D7's "kept across a disable"): unbounded bookkeeping first
(confirm or withdraw anything sitting in "removing", re-resolve any
origin a UIDVALIDITY resync orphaned, repair split-thread groupings, and
progress anything left mid-flight), then a list of guards that skip the
account entirely rather than claim anything new, then at most
`glacier.batch_size` new messages.

The guards are deliberately conservative: several of them are the exact
predicates other destructive operations in this codebase already use
to decide "the mirror cannot be trusted right now" (a pending move, an
unsynced folder, an unacknowledged failure) -- reused here rather than
re-derived, since re-deriving the same conclusion at a second site is
exactly the kind of silent drift the project's own conventions warn
against.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import text

from mail_verdict.glacier.operations import (
    _ELIGIBILITY_SQL,
    confirm_or_withdraw_removing,
    copy_message,
    expunge_message,
    repair_thread_ids,
    reresolve_origins,
    resolve_duplicate,
    verify_message,
)
from mail_verdict.glacier.restore import (
    confirm_restores,
    fail_stale_restores,
    require_message_append_support,
)
from mail_verdict.queue.notify import ReconciliationTimer

if TYPE_CHECKING:
    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.config.loader import GlacierConfig
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

# test_lock_keys.py asserts every *_LOCK_KEY constant in the package is
# pairwise distinct -- it, not this comment, is what actually enforces
# that this value is free.
_SWEEP_LOCK_KEY = 761_035_100

# Bookkeeping (confirm/withdraw, re-resolve, thread repair, progressing
# anything mid-flight) never destroys a message on its own -- the actual
# claim-and-glacier step is what needs the small, paced batch.
_BOOKKEEPING_BATCH_SIZE = 500


async def _write_hazard_reason(db: DatabaseConnection, account_id: uuid.UUID) -> str | None:
    """
    Whether the mirror is currently trustworthy enough for a *destructive*
    glacier step (an EXPUNGE) to run against this account at all, or None
    if it is safe to proceed. Deliberately narrower than
    _sweep_guard_reason: whether automatic sweeping is even configured
    for this account has no bearing on whether it is safe to finish a
    row someone already glaciered by hand and left mid-flight (a crash,
    or a verify that did not pass first time) -- this module's own
    docstring documents that such rows are seen through regardless of
    whether the automatic sweep itself is enabled. Reused by both
    _sweep_guard_reason (which layers its own claim-new-work checks on
    top) and _progress_mid_flight, which is exactly the destructive step
    that must never bypass it.

    Args:
        db: Database connection
        account_id: Account to check

    Returns:
        A short reason the mirror cannot currently be trusted, or None
    """
    async with db.session() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT a.is_active, a.state,
                           EXISTS (
                             SELECT 1 FROM messages m
                             WHERE m.account_id = a.id AND m.imap_uid IS NULL
                               AND m.expunged_at IS NULL
                           ) AS has_pending_move,
                           EXISTS (
                             SELECT 1 FROM sync_notifications sn
                             WHERE sn.account_id = a.id AND sn.acknowledged_at IS NULL
                           ) AS has_unacknowledged
                    FROM accounts a WHERE a.id = :account_id
                    """
                ),
                {"account_id": account_id},
            )
        ).mappings().one_or_none()
        if row is None:
            return "account no longer exists"
        if not row["is_active"] or row["state"] != "active":
            return "account is not currently active"
        if row["has_pending_move"]:
            return "a move is still pending on this account -- the mirror is untrustworthy"
        if row["has_unacknowledged"]:
            return "an unacknowledged sync failure exists on this account"
    return None


async def _sweep_guard_reason(
    db: DatabaseConnection, account_id: uuid.UUID, *, cfg: GlacierConfig,
) -> str | None:
    """
    Section 8.3 step 2: why this account's automatic sweep should claim
    nothing new this tick, or None if it may proceed.
    """
    capability_error = await require_message_append_support(db)
    if capability_error is not None:
        return capability_error

    async with db.session() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT ap.glacier_enabled, ap.glacier_auto_days, ap.glacier_folder_id,
                           ss.last_full_sync,
                           (
                             SELECT count(*) FROM glacier_messages g
                             WHERE g.account_id = ap.account_id AND g.state = 'removing'
                           ) AS removing_count
                    FROM account_prefs ap
                    LEFT JOIN sync_state ss ON ss.account_id = ap.account_id
                    WHERE ap.account_id = :account_id
                    """
                ),
                {"account_id": account_id},
            )
        ).mappings().one_or_none()
        if row is None:
            return "no account_prefs row"
        if not row["glacier_enabled"] or row["glacier_auto_days"] is None:
            return "automatic sweep is off for this account"
        if row["glacier_folder_id"] is None:
            return "glacier has never been enabled"
        if row["last_full_sync"] is None:
            return "account has never completed a sync pass"
        if row["removing_count"] >= cfg.max_unconfirmed:
            return f"{row['removing_count']} messages already unconfirmed in the removing state"

        archive_ok = (
            await session.execute(
                text(
                    """
                    SELECT bool_or(f.initial_sync_done AND f.deleted_at IS NULL) AS ok
                    FROM folders f
                    LEFT JOIN folder_prefs fp ON fp.folder_id = f.id
                    WHERE f.account_id = :account_id
                      AND coalesce(fp.special_use_override, f.special_use, '') = 'archive'
                    """
                ),
                {"account_id": account_id},
            )
        ).scalar_one_or_none()
        if not archive_ok:
            return "no fully-synced archive folder on this account"

    return await _write_hazard_reason(db, account_id)


async def _claim_archive_candidates(
    db: DatabaseConnection, account_id: uuid.UUID, *, auto_days: int, batch_size: int,
) -> list[uuid.UUID]:
    """
    Section 8.2's "what counts as the archive": folders whose *effective*
    special use is 'archive' -- the same predicate the retention sweep
    already uses for its own roles. Sub-folders that do not themselves
    carry the flag are not swept, a deliberate limit rather than an
    oversight. Age is judged against the message's own received_at
    (D11's decision), never the time it has sat archived.
    """
    async with db.session() as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT m.id
                    FROM messages m
                    JOIN folders f ON f.id = m.folder_id
                    JOIN accounts a ON a.id = m.account_id
                    LEFT JOIN folder_prefs fp ON fp.folder_id = f.id
                    WHERE m.account_id = :account_id
                      AND coalesce(fp.special_use_override, f.special_use, '') = 'archive'
                      AND {_ELIGIBILITY_SQL}
                      AND m.received_at IS NOT NULL
                      AND m.received_at < now() - make_interval(days => :auto_days)
                    ORDER BY m.received_at
                    LIMIT :batch
                    """
                ),
                {"account_id": account_id, "auto_days": auto_days, "batch": batch_size},
            )
        ).mappings().all()
    return [row["id"] for row in rows]


async def _progress_mid_flight(
    db: DatabaseConnection, account_id: uuid.UUID, *, event_ring: EventRing | None,
) -> None:
    """Push anything left `copied` toward `verified`, and anything
    `verified` toward `glaciered` -- a crashed process, or a manual move
    that only got partway, resumes here rather than staying stuck.

    🚨 Verifying is read-only against the mirror -- a corrupted or
    inconsistent comparison simply fails to verify (verify_message's own
    guard), never destroys anything -- so it always runs. Expunging is
    the step that actually removes a message from the server, and it
    must never run while the account-wide write hazard applies (a
    pending move elsewhere, an unacknowledged failure, the account not
    currently connected): _write_hazard_reason is the same check
    _sweep_guard_reason uses to decide whether to claim new work, reused
    here because "the mirror cannot be trusted right now" does not stop
    being true just because this row was claimed on an earlier tick.
    """
    async with db.session() as session:
        copied_ids = (
            await session.execute(
                text(
                    "SELECT id FROM glacier_messages WHERE account_id = :account_id "
                    "AND state = 'copied' LIMIT :batch"
                ),
                {"account_id": account_id, "batch": _BOOKKEEPING_BATCH_SIZE},
            )
        ).scalars().all()
    for glacier_id in copied_ids:
        await verify_message(db, glacier_id)

    if await _write_hazard_reason(db, account_id) is not None:
        return

    async with db.session() as session:
        verified_ids = (
            await session.execute(
                text(
                    "SELECT id FROM glacier_messages WHERE account_id = :account_id "
                    "AND state = 'verified' AND origin_message_id IS NOT NULL LIMIT :batch"
                ),
                {"account_id": account_id, "batch": _BOOKKEEPING_BATCH_SIZE},
            )
        ).scalars().all()
    for glacier_id in verified_ids:
        await expunge_message(db, glacier_id, event_ring=event_ring)


async def _sweep_account_once(
    db: DatabaseConnection, account_id: uuid.UUID, *, event_ring: EventRing | None,
    cfg: GlacierConfig,
) -> None:
    confirmed, withdrawn = await confirm_or_withdraw_removing(
        db, account_id, grace_seconds=cfg.confirm_grace_seconds,
    )
    await reresolve_origins(db, account_id)
    await repair_thread_ids(db, account_id)
    await _progress_mid_flight(db, account_id, event_ring=event_ring)
    await confirm_restores(db, account_id, event_ring=event_ring)
    await fail_stale_restores(db, account_id, timeout_seconds=cfg.restore_timeout_seconds)
    if confirmed or withdrawn:
        logger.info(
            "Glacier bookkeeping",
            extra={"account_id": str(account_id), "confirmed": confirmed, "withdrawn": withdrawn},
        )

    reason = await _sweep_guard_reason(db, account_id, cfg=cfg)
    if reason is not None:
        logger.debug(
            "Glacier sweep skipping account",
            extra={"account_id": str(account_id), "reason": reason},
        )
        return

    async with db.session() as session:
        auto_days = (
            await session.execute(
                text("SELECT glacier_auto_days FROM account_prefs WHERE account_id = :id"),
                {"id": account_id},
            )
        ).scalar_one()

    candidates = await _claim_archive_candidates(
        db, account_id, auto_days=auto_days, batch_size=cfg.batch_size,
    )
    glaciered = 0
    for message_id in candidates:
        outcome = await copy_message(db, message_id)
        if outcome.status == "duplicate_removable" and outcome.glacier_id is not None:
            await resolve_duplicate(db, message_id, outcome.glacier_id)
            continue
        if outcome.status in ("ineligible", "duplicate_conflict"):
            continue
        if outcome.glacier_id is None:
            continue
        if not await verify_message(db, outcome.glacier_id):
            continue
        if await expunge_message(db, outcome.glacier_id, event_ring=event_ring) == "expunged":
            glaciered += 1
    if glaciered:
        logger.info(
            "Automatic glacier sweep",
            extra={"account_id": str(account_id), "glaciered": glaciered},
        )


async def _sweep_once(
    db: DatabaseConnection, *, event_ring: EventRing | None, cfg: GlacierConfig,
) -> None:
    if not cfg.sweep_enabled:
        return
    async with db.session() as session:
        account_ids = (
            await session.execute(
                text("SELECT account_id FROM account_prefs WHERE glacier_folder_id IS NOT NULL")
            )
        ).scalars().all()
    for account_id in account_ids:
        await _sweep_account_once(db, account_id, event_ring=event_ring, cfg=cfg)


def build_glacier_sweep_timer(
    db: DatabaseConnection, event_ring: EventRing | None, cfg: GlacierConfig,
) -> ReconciliationTimer:
    """The advisory-locked periodic pass driving both the automatic
    sweep and the bookkeeping every glaciered account needs regardless
    of whether the automatic sweep is on for it."""

    async def _callback() -> None:
        await _sweep_once(db, event_ring=event_ring, cfg=cfg)

    return ReconciliationTimer(db, _SWEEP_LOCK_KEY, _callback, cfg.interval_seconds)
