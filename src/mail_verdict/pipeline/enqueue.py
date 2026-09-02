"""
Turning a live message arrival into an embedding enqueue, the gate that
gives that embedding's terminal state a `pipeline_runs` row, and the
watermark and reconciliation that cover the gap a listener reconnect
leaves behind.

Embedding strictly precedes the pipeline: `enqueue_live_arrival` enqueues
a `message_embeddings` row, never a `pipeline_runs` row directly. Only
`enqueue_pipeline_run_if_live_eligible`, called from
embeddings/repository.py inside the same transaction that moves an
embedding to 'done' or 'failed', ever inserts into `pipeline_runs`. Both
provider calls (embedding and classification) hit the same account, so
gating on the first costs no real availability -- if the provider is
down, nothing downstream was going to be classified either -- and it buys
the invariant that everything in the pipeline queue has a vector. A
message whose embedding permanently fails still reaches the gate on that
failure, so it is still classified, just without neighbour hints.

The pipeline is triggered by arrival only: `message`/`insert` with
`origin = "sync"`, and nothing else -- never a `message`/`update`. A
stage reacting to a folder-move update would also see the update its own
Move effect just made, since `origin` distinguishes PostIMAP's writes
from this application's, not the pipeline's own write from a user's a
moment later; a stage that reacted to moves would be one edit away from
looping on itself. Reacting to arrival only removes the possibility
rather than guarding against it.

Reconciliation exists because a listener reconnect loses any NOTIFY fired
during the gap (postimap/listener.py's own docstring says so). A
set-difference query finds what was missed, but a set-difference query
alone cannot tell "arrived while disconnected" (must be enqueued) from
"historical mail" (must never be) -- that is what the watermark in
pipeline_folder_state is for. It also respects the embedding gate: it
only enqueues a run for a message whose current-model embedding has
already reached a terminal state, never for one still pending.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import text

from mail_verdict.database.msg_key import compute_msg_key
from mail_verdict.embeddings.provider import DEFAULT_EMBEDDING_MODEL
from mail_verdict.queue.notify import ReconciliationTimer, WorkQueueNotifier

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

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


async def enqueue_live_arrival(
    db: DatabaseConnection, event: PostimapEvent, settings_service: SettingsService,
) -> None:
    """
    Enqueue an embedding for a message that just arrived, if it is
    embeddable at all. Called directly from the `message`/`insert` branch
    of the postimap_events dispatcher -- never from an `update`.

    Deliberately not the pipeline-scope gate (sent/drafts/trash/junk/
    archive): embeddings serve semantic search across every folder, the
    same breadth the backfill reconciler already gives them
    (embeddings/worker.py). The narrower pipeline scope is enforced once,
    at the second enqueue in enqueue_pipeline_run_if_live_eligible, so it
    cannot drift between the two gates.

    Priority 0, ahead of the backfill sweep's 100, so live mail's
    embedding -- and everything waiting on it -- is never stuck behind a
    backlog.
    """
    from mail_verdict.embeddings.repository import EmbeddingRepository

    try:
        message_id = uuid.UUID(event.id)
        account_id = uuid.UUID(event.account_id)
    except ValueError:
        logger.warning("Invalid message insert payload: %s", event)
        return

    semantic_settings = (
        settings_service.get("semantic") if settings_service.has_category("semantic") else {}
    )
    model = str(semantic_settings.get("model", DEFAULT_EMBEDDING_MODEL))

    embedding_repo = EmbeddingRepository(db)
    inserted = await embedding_repo.enqueue_one(
        account_id=account_id, message_id=message_id, model=model, priority=0,
    )
    if inserted:
        async with db.session() as session:
            await WorkQueueNotifier.notify(session, "embeddings")


async def enqueue_pipeline_run_if_live_eligible(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    message_id: uuid.UUID | None,
    settings_service: SettingsService,
) -> bool:
    """
    The pipeline half of the embedding gate. Called by
    embeddings/repository.py inside the same transaction that moves a
    message_embeddings row to 'done' or 'failed' -- never called directly,
    and never opens its own session, so the two writes commit or roll back
    together and the second enqueue can never be lost independently of the
    first.

    Live-eligible mirrors _reconcile_once's own definition exactly, since
    both answer the same question at different moments: the message's
    folder carries our watermark (pipeline_folder_state), the message
    arrived after it, and it is not older than
    pipeline.live_max_age_days -- the secondary guard against a missing or
    stale watermark reclassifying old mail. A message that fails this
    check produces no row and no explanation, the same "never existed"
    convention the old direct enqueue used, since scope was never in
    doubt (see the module docstring).

    Returns:
        True if a new row was inserted -- the caller notifies "pipeline"
    """
    if message_id is None:
        return False

    pipeline_settings = (
        settings_service.get("pipeline") if settings_service.has_category("pipeline") else {}
    )
    max_age_days = int(pipeline_settings.get("live_max_age_days", 7))

    row = await session.execute(
        text(
            """
            SELECT m.message_id AS message_id_hdr, m.from_addr, m.subject,
                   m.received_at, m.size_bytes
            FROM messages m
            JOIN folders f ON f.id = m.folder_id
            JOIN pipeline_folder_state s ON s.folder_id = f.id
            LEFT JOIN folder_prefs fp ON fp.folder_id = f.id
            WHERE m.id = :message_id AND m.account_id = :account_id
              AND m.expunged_at IS NULL AND m.is_draft = false AND f.deleted_at IS NULL
              AND coalesce(fp.special_use_override, f.special_use, '') NOT IN
                  ('sent', 'drafts', 'trash', 'junk', 'archive')
              AND s.backfill_completed_at IS NOT NULL
              AND m.created_at > s.backfill_completed_at
              AND m.received_at > now() - make_interval(days => :max_age_days)
            """
        ),
        {"message_id": message_id, "account_id": account_id, "max_age_days": max_age_days},
    )
    result = row.one_or_none()
    if result is None:
        return False

    msg_key = compute_msg_key(
        account_id=account_id, message_id_hdr=result.message_id_hdr, from_addr=result.from_addr,
        subject=result.subject, received_at=result.received_at, size_bytes=result.size_bytes,
    )

    # A UIDVALIDITY resync fires an ordinary message/insert for mail that
    # already has a run under the old message id -- not suppressed by
    # PostIMAP the way a folder's first sync is (see the consumer
    # contract's backfill-suppression section). msg_key is unchanged, so
    # ON CONFLICT still finds the existing row; DO UPDATE repoints its
    # message_id at the new one instead of DO NOTHING silently leaving it
    # stale, which is what GET /api/mails/{id}/runs joins on.
    insert_result = await session.execute(
        text(
            """
            INSERT INTO pipeline_runs
                (account_id, msg_key, message_id, from_addr, dedup_key, origin, apply, priority)
            VALUES (:account_id, :msg_key, :message_id, :from_addr, 'live', 'live', true, 0)
            ON CONFLICT (account_id, msg_key, dedup_key, (coalesce(from_addr, ''))) DO UPDATE
                SET message_id = EXCLUDED.message_id
                WHERE pipeline_runs.message_id IS DISTINCT FROM EXCLUDED.message_id
            RETURNING (xmax = 0) AS was_inserted
            """
        ),
        {
            "account_id": account_id, "msg_key": msg_key, "message_id": message_id,
            "from_addr": result.from_addr,
        },
    )
    row_result = insert_result.one_or_none()
    return bool(row_result is not None and row_result.was_inserted)


async def record_folder_watermark(db: DatabaseConnection, event: PostimapEvent) -> None:
    """
    Record the watermark for a folder's first full sync completing.

    Called from the `folder`/`sync_complete` branch of the postimap_events
    dispatcher when `event.backfill` is true -- the one-time signal that
    this folder's backfill just finished, per PostIMAP's consumer
    contract. A later resync of an already-synced folder does not repeat
    this event, which is exactly right: the watermark should never move
    backward or reset.

    The watermark is read from `folders.last_synced_at` rather than taken
    as `now()` on this side. This handler runs asynchronously -- NOTIFY
    delivery plus the listener's own dispatch queue -- so `now()` here is
    always later than the moment PostIMAP actually finished the backfill,
    by however long that gap happens to be. Any message PostIMAP inserts
    in that gap gets a `created_at` earlier than a watermark taken from
    this side's clock and is permanently excluded from
    `enqueue_pipeline_run_if_live_eligible` and `_reconcile_once` alike,
    since both compare against the same stored value and it is never
    moved once set.

    `last_synced_at` is PostIMAP's own clock, and unlike `updated_at` it
    is not touched by the per-message trigger that maintains
    `total_count`/`unread_count` on every message insert -- PostIMAP sets
    it once per sync cycle, in `updateFolderState`, before the
    `initial_sync_done` flip that fires this event, and not again until
    the folder's next periodic cycle. A live message landing in the gap
    this handler is racing against therefore cannot have already pushed
    the watermark forward to its own arrival time the way `updated_at`
    would.
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
                SELECT f.id, f.account_id, f.last_synced_at
                FROM folders f
                WHERE f.id = :folder_id AND f.account_id = :account_id
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
    Find live-eligible messages with a terminal embedding and no pipeline
    run yet, and enqueue them -- the gap a listener reconnect leaves,
    bounded to one batch per tick so a very large gap does not hold the
    advisory lock for long.

    Live-eligible: the message's folder has a watermark, the message
    arrived after it, and it is not older than `pipeline.live_max_age_days`
    -- a secondary guard against a missing or stale watermark quietly
    reclassifying an entire mailbox. The same definition
    enqueue_pipeline_run_if_live_eligible applies at the embedding's own
    terminal transition; this query additionally requires that terminal
    state to already exist, since a listener gap can just as easily have
    lost the message's arrival before its embedding was ever enqueued --
    the embeddings backfill reconciler (embeddings/worker.py) is what
    eventually gives it one, and this pass then finds it on a later tick.
    A message whose embedding is still pending is left for that
    transition to enqueue instead, never enqueued here ahead of it.
    """
    pipeline_settings = (
        settings_service.get("pipeline") if settings_service.has_category("pipeline") else {}
    )
    max_age_days = int(pipeline_settings.get("live_max_age_days", 7))
    semantic_settings = (
        settings_service.get("semantic") if settings_service.has_category("semantic") else {}
    )
    embedding_model = str(semantic_settings.get("model", DEFAULT_EMBEDDING_MODEL))

    async with db.session() as session:
        rows = await session.execute(
            text(
                """
                SELECT m.id, m.account_id, m.message_id, m.from_addr, m.subject,
                       m.received_at, m.size_bytes
                FROM messages m
                JOIN folders f ON f.id = m.folder_id
                JOIN pipeline_folder_state s ON s.folder_id = f.id
                JOIN message_embeddings me ON me.account_id = m.account_id
                    AND me.message_id = m.id AND me.model = :embedding_model
                    AND me.status IN ('done', 'failed')
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
            {
                "max_age_days": max_age_days, "embedding_model": embedding_model,
                "batch": _RECONCILE_BATCH_SIZE,
            },
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
                        (account_id, msg_key, message_id, from_addr, dedup_key, origin,
                         apply, priority)
                    VALUES (:account_id, :msg_key, :message_id, :from_addr, 'live', 'live',
                            true, 0)
                    ON CONFLICT (account_id, msg_key, dedup_key, (coalesce(from_addr, '')))
                        DO UPDATE SET message_id = EXCLUDED.message_id
                        WHERE pipeline_runs.message_id IS DISTINCT FROM EXCLUDED.message_id
                    RETURNING (xmax = 0) AS was_inserted
                    """
                ),
                {
                    "account_id": row.account_id, "msg_key": msg_key, "message_id": row.id,
                    "from_addr": row.from_addr,
                },
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
