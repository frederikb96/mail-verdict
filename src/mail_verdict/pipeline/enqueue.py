"""
Turning a live message arrival into a `pipeline_runs` row, and the
watermark and reconciliation that cover the gap a listener reconnect
leaves behind.

The pipeline is triggered by arrival only: `message`/`insert` with
`origin = "sync"`, and nothing else. This is deliberately narrower than
the old rules engine, which mapped every `message`/`update` to a
`mail.moved` trigger -- including the update the pipeline's own Move
effect just made, with nothing able to tell that write from a user's. A
stage reacting to a move is a loop one edit away; reacting to arrival only
removes the possibility rather than guarding against it.

Reconciliation exists because a listener reconnect loses any NOTIFY fired
during the gap (postimap/listener.py's own docstring says so). A
set-difference query finds what was missed, but a set-difference query
alone cannot tell "arrived while disconnected" (must be enqueued) from
"historical mail" (must never be) -- that is what the watermark in
pipeline_folder_state is for.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import text

from mail_verdict.database.msg_key import compute_msg_key
from mail_verdict.queue.notify import ReconciliationTimer, WorkQueueNotifier

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.postimap.listener import PostimapEvent
    from mail_verdict.settings.service import SettingsService

logger = logging.getLogger(__name__)

# Kept in sync with pipeline/runner.py's re-check at execution time.
_SKIP_FOLDER_SPECIAL_USE = frozenset({"sent", "drafts", "trash", "junk", "archive"})

# Distinct from queue/manager.py's own reclaim-timer lock key (761_034_221)
# and from any future sweep controller's -- every ReconciliationTimer in
# the process must pick a key nothing else uses.
_RECONCILE_LOCK_KEY = 761_034_331

_DEFAULT_RECONCILE_INTERVAL_S = 30.0
_RECONCILE_BATCH_SIZE = 200


async def enqueue_live_arrival(db: DatabaseConnection, event: PostimapEvent) -> None:
    """
    Enqueue a pipeline run for a message that just arrived, if it is in
    scope. Called directly from the `message`/`insert` branch of the
    postimap_events dispatcher -- never from an `update`.

    A message out of scope (sent/drafts/trash/junk/archive, or a draft)
    produces no row at all, which is deliberate: a run that never existed
    needs no explanation, whereas a run that did nothing still has to say
    why (see pipeline/runner.py's scope re-check for the symmetric case).
    """
    try:
        message_id = uuid.UUID(event.id)
        account_id = uuid.UUID(event.account_id)
    except ValueError:
        logger.warning("Invalid message insert payload: %s", event)
        return

    async with db.session() as session:
        row = await session.execute(
            text(
                """
                SELECT m.account_id, m.message_id, m.from_addr, m.subject, m.received_at,
                       m.size_bytes, m.is_draft, m.expunged_at,
                       coalesce(fp.special_use_override, f.special_use) AS effective_special_use,
                       f.deleted_at AS folder_deleted_at
                FROM messages m
                JOIN folders f ON f.id = m.folder_id
                LEFT JOIN folder_prefs fp ON fp.folder_id = f.id
                WHERE m.id = :message_id AND m.account_id = :account_id
                """
            ),
            {"message_id": message_id, "account_id": account_id},
        )
        result = row.one_or_none()
        if result is None:
            return
        if (
            result.is_draft
            or result.expunged_at is not None
            or result.folder_deleted_at is not None
            or (result.effective_special_use or "") in _SKIP_FOLDER_SPECIAL_USE
        ):
            return

        msg_key = compute_msg_key(
            account_id=account_id, message_id_hdr=result.message_id, from_addr=result.from_addr,
            subject=result.subject, received_at=result.received_at, size_bytes=result.size_bytes,
        )

        # A UIDVALIDITY resync fires an ordinary message/insert for mail
        # that already has a run under the old message id -- not
        # suppressed by PostIMAP the way a folder's first sync is (see
        # the consumer contract's backfill-suppression section). msg_key
        # is unchanged, so ON CONFLICT still finds the existing row; DO
        # UPDATE repoints its message_id at the new one instead of
        # DO NOTHING silently leaving it stale, which is what
        # GET /api/mails/{id}/runs joins on.
        insert_result = await session.execute(
            text(
                """
                INSERT INTO pipeline_runs
                    (account_id, msg_key, message_id, dedup_key, origin, apply, priority)
                VALUES (:account_id, :msg_key, :message_id, 'live', 'live', true, 0)
                ON CONFLICT (account_id, msg_key, dedup_key) DO UPDATE
                    SET message_id = EXCLUDED.message_id
                    WHERE pipeline_runs.message_id IS DISTINCT FROM EXCLUDED.message_id
                RETURNING (xmax = 0) AS was_inserted
                """
            ),
            {"account_id": account_id, "msg_key": msg_key, "message_id": message_id},
        )
        row_result = insert_result.one_or_none()
        if row_result is not None and row_result.was_inserted:
            await WorkQueueNotifier.notify(session, "pipeline")


async def record_folder_watermark(db: DatabaseConnection, event: PostimapEvent) -> None:
    """
    Record the watermark for a folder's first full sync completing.

    Called from the `folder`/`sync_complete` branch of the postimap_events
    dispatcher when `event.backfill` is true -- the one-time signal that
    this folder's backfill just finished, per PostIMAP's consumer
    contract. A later resync of an already-synced folder does not repeat
    this event, which is exactly right: the watermark should never move
    backward or reset.
    """
    if not event.backfill:
        return
    try:
        folder_id = uuid.UUID(event.folder_id) if event.folder_id else uuid.UUID(event.id)
        account_id = uuid.UUID(event.account_id)
    except ValueError:
        logger.warning("Invalid folder sync_complete payload: %s", event)
        return

    async with db.session() as session:
        await session.execute(
            text(
                """
                INSERT INTO pipeline_folder_state (folder_id, account_id, backfill_completed_at)
                VALUES (:folder_id, :account_id, now())
                ON CONFLICT (folder_id) DO UPDATE
                    SET backfill_completed_at = COALESCE(
                        pipeline_folder_state.backfill_completed_at, EXCLUDED.backfill_completed_at
                    )
                """
            ),
            {"folder_id": folder_id, "account_id": account_id},
        )


async def _reconcile_once(db: DatabaseConnection, settings_service: SettingsService) -> None:
    """
    Find live-eligible messages with no pipeline run yet and enqueue them
    -- the gap a listener reconnect leaves, bounded to one batch per tick
    so a very large gap does not hold the advisory lock for long.

    Live-eligible: the message's folder has a watermark, the message
    arrived after it, and it is not older than `pipeline.live_max_age_days`
    -- a secondary guard against a missing or stale watermark quietly
    reclassifying an entire mailbox.
    """
    pipeline_settings = (
        settings_service.get("pipeline") if settings_service.has_category("pipeline") else {}
    )
    max_age_days = int(pipeline_settings.get("live_max_age_days", 7))

    async with db.session() as session:
        rows = await session.execute(
            text(
                """
                SELECT m.id, m.account_id, m.message_id, m.from_addr, m.subject,
                       m.received_at, m.size_bytes
                FROM messages m
                JOIN folders f ON f.id = m.folder_id
                JOIN pipeline_folder_state s ON s.folder_id = f.id
                LEFT JOIN folder_prefs fp ON fp.folder_id = f.id
                WHERE m.expunged_at IS NULL
                  AND m.is_draft = false
                  AND f.deleted_at IS NULL
                  AND coalesce(fp.special_use_override, f.special_use, '') NOT IN
                      ('sent', 'drafts', 'trash', 'junk', 'archive')
                  AND s.backfill_completed_at IS NOT NULL
                  AND m.created_at > s.backfill_completed_at
                  AND m.received_at > now() - make_interval(days => :max_age_days)
                  AND NOT EXISTS (
                        SELECT 1 FROM pipeline_runs r
                        WHERE r.account_id = m.account_id AND r.message_id = m.id
                            AND r.dedup_key = 'live'
                  )
                ORDER BY m.received_at
                LIMIT :batch
                """
            ),
            {"max_age_days": max_age_days, "batch": _RECONCILE_BATCH_SIZE},
        )
        candidates = rows.all()
        if not candidates:
            return

        # The anti-join above is on message_id, which is cheap but wrong
        # for the resync case: msg_key is the durable identity, not
        # message_id, and computing it requires the hash fallback's exact
        # Python algorithm for a message with no Message-ID header (see
        # database/msg_key.py) -- reimplementing that in SQL would be a
        # second, driftable definition of the same key. So the candidate
        # set is filtered cheaply in SQL, then msg_key is computed in
        # Python per candidate and the real dedup happens at the INSERT's
        # ON CONFLICT. A conflict here means this candidate already has a
        # run under a different message_id (the UIDVALIDITY case): DO
        # UPDATE repoints it, which is what makes this query's anti-join
        # converge on the next tick instead of reselecting the row forever.
        newly_inserted = 0
        for row in candidates:
            msg_key = compute_msg_key(
                account_id=row.account_id, message_id_hdr=row.message_id,
                from_addr=row.from_addr, subject=row.subject,
                received_at=row.received_at, size_bytes=row.size_bytes,
            )
            result = await session.execute(
                text(
                    """
                    INSERT INTO pipeline_runs
                        (account_id, msg_key, message_id, dedup_key, origin, apply, priority)
                    VALUES (:account_id, :msg_key, :message_id, 'live', 'live', true, 0)
                    ON CONFLICT (account_id, msg_key, dedup_key) DO UPDATE
                        SET message_id = EXCLUDED.message_id
                        WHERE pipeline_runs.message_id IS DISTINCT FROM EXCLUDED.message_id
                    RETURNING (xmax = 0) AS was_inserted
                    """
                ),
                {"account_id": row.account_id, "msg_key": msg_key, "message_id": row.id},
            )
            row_result = result.one_or_none()
            if row_result is not None and row_result.was_inserted:
                newly_inserted += 1

        if newly_inserted:
            await WorkQueueNotifier.notify(session, "pipeline")
        logger.info(
            "Reconciliation pass",
            extra={"candidates": len(candidates), "newly_enqueued": newly_inserted},
        )


def build_reconciliation_timer(
    db: DatabaseConnection, settings_service: SettingsService,
) -> ReconciliationTimer:
    """The advisory-locked periodic pass -- one per process, safe with
    more than one replica because only the lock holder runs the query."""

    async def _callback() -> None:
        await _reconcile_once(db, settings_service)

    interval = _DEFAULT_RECONCILE_INTERVAL_S
    return ReconciliationTimer(db, _RECONCILE_LOCK_KEY, _callback, interval)
