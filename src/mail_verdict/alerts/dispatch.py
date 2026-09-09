"""
Turning a live mail arrival into an alert -- called directly from the
`message`/`insert` branch of server.py's postimap event dispatcher,
gated the same way enqueue_live_arrival is: origin == "sync" only, never
a backfill, so historical mail synced for the first time is never turned
into an alert any more than it is ever classified.

A message a rule can still refile (anything the pipeline actually runs
against) is staged rather than delivered: inserted with delivered_at left
NULL and folder_id holding the arrival folder as a placeholder only.
finalize_pending_mail_alerts_once() -- the periodic pass this module also
builds -- delivers it once that message's pipeline run reaches a terminal
status, or once a bounded wait (settings.mail.notify_wait_seconds)
expires, whichever comes first, and overwrites folder_id with wherever
the message actually is by then. That is the fix for the ordering bug: a
message a rule moves out of the inbox used to announce itself, and apply
a per-folder notification preference, against the folder it merely
arrived in.

A message no pipeline run will ever exist for -- arriving directly into
a folder the pipeline never runs against (sent/drafts/trash/junk/
archive), a folder with no watermark yet (pipeline/enqueue.py's own
is_live_pipeline_possible re-derives the whole eligibility predicate,
not just the folder's role, for exactly this reason), or mail older than
pipeline.live_max_age_days -- skips staging entirely and is delivered
immediately, the same as before this module grew a staged path: no
pipeline run will ever reach a terminal status for it, so waiting could
only ever mean waiting out the bound.

Either path funnels through AlertRepository.create_mail_alert's single
ON CONFLICT DO NOTHING on dedupe_key -- the entire fires-exactly-once
guarantee, unchanged by staging. A message can only ever take one of the
two paths for a given arrival (the folder_id on that one insert event
decides it, once), and finalize_pending_mail_alerts_once() only ever
delivers a still-NULL delivered_at once, inside the same UPDATE that
stamps it -- so neither path, nor two finalizer ticks racing under the
advisory lock ReconciliationTimer already holds, can produce two
notifications for one arrival.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, NamedTuple

from sqlalchemy import delete, select, text, update

from mail_verdict.database.models import Alert, Message
from mail_verdict.database.msg_key import compute_msg_key
from mail_verdict.database.repository import AlertRepository
from mail_verdict.pipeline.enqueue import is_live_pipeline_possible
from mail_verdict.push.send import dispatch_push_for_alert
from mail_verdict.queue.notify import ReconciliationTimer

if TYPE_CHECKING:
    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.push.vapid import VapidKeyRepository
    from mail_verdict.settings.service import SettingsService

logger = logging.getLogger(__name__)

# A sender line long enough to be useful, short enough that a bell
# dropdown row stays one line -- the same rough shape a notification
# popup itself would truncate to.
_BODY_MAX_LEN = 120

# Kept in sync with pipeline/enqueue.py's own gate (and pipeline/runner.py's
# re-check of it) -- a message in one of these can never produce a
# pipeline_runs row, so waiting for one would wait out the bound every time.
_SKIP_FOLDER_SPECIAL_USE = frozenset({"sent", "drafts", "trash", "junk", "archive"})

# Distinct from pipeline/enqueue.py's _RECONCILE_LOCK_KEY (761_034_331) and
# outbox/pending.py's _PENDING_SEND_LOCK_KEY (761_034_500) -- every
# ReconciliationTimer in the process needs its own key.
_FINALIZE_LOCK_KEY = 761_034_600

# A staged alert's terminal status (or expunged message) can arrive at any
# moment, so this is tuned for responsiveness the way outbox/pending.py's
# own 1s poll is for its much shorter grace window -- an implementation
# constant, not something a person would tune, unlike
# settings.mail.notify_wait_seconds itself.
_FINALIZE_POLL_SECONDS = 2.0
_FINALIZE_BATCH_SIZE = 100

# asyncio holds only a weak reference to a task nothing else is holding
# a reference to, so a fire-and-forget push dispatch can be garbage
# collected mid-flight the moment this function returns. Kept here for
# exactly as long as the task runs, and discarded via its own done
# callback once it finishes.
_background_push_tasks: set[asyncio.Task[None]] = set()


class _ArrivalFacts(NamedTuple):
    msg_key: str
    title: str
    body: str | None


async def _load_arrival_facts(
    db: DatabaseConnection, account_id: uuid.UUID, message_id: uuid.UUID,
) -> _ArrivalFacts | None:
    """Read what an alert needs from the message row -- None if the
    message is already gone (expunged between the insert and this call),
    the same "nothing to build an alert about" case whichever path is
    creating one."""
    async with db.session() as session:
        row = (
            await session.execute(
                select(
                    Message.message_id, Message.from_addr, Message.subject,
                    Message.received_at, Message.size_bytes,
                ).where(Message.id == message_id)
            )
        ).one_or_none()
    if row is None:
        return None

    body = row.from_addr or None
    if body and len(body) > _BODY_MAX_LEN:
        body = body[:_BODY_MAX_LEN] + "…"
    return _ArrivalFacts(
        msg_key=compute_msg_key(
            account_id=account_id, message_id_hdr=row.message_id, from_addr=row.from_addr,
            subject=row.subject, received_at=row.received_at, size_bytes=row.size_bytes,
        ),
        title=row.subject or "(no subject)",
        body=body,
    )


async def create_mail_alert_for_arrival(
    db: DatabaseConnection,
    event_ring: EventRing | None,
    vapid_repo: VapidKeyRepository | None = None,
    *,
    account_id: uuid.UUID,
    message_id: uuid.UUID,
    settings_service: SettingsService,
    folder_id: uuid.UUID | None = None,
) -> None:
    """
    Turn a newly-arrived message into an alert -- delivered immediately if
    no pipeline run will ever exist for it (is_live_pipeline_possible,
    pipeline/enqueue.py -- the same predicate the pipeline's own live-
    arrival enqueue checks, not merely the folder's role: a message can
    just as well be permanently excluded by a missing watermark or the
    age limit as by arriving straight into Junk), staged otherwise. See
    this module's own docstring for why, and
    finalize_pending_mail_alerts_once() for the other half of the staged
    path.

    folder_id rides along on the live event and, for the immediate path,
    the push dispatch and the row itself -- the same threaded-through
    value described in create_mail_alert_for_arrival's history (see
    AlertRepository.create_mail_alert). For the staged path it is only
    ever a placeholder, overwritten once the message's real folder is
    known.
    """
    possible = await is_live_pipeline_possible(
        db, account_id=account_id, message_id=message_id, settings_service=settings_service,
    )
    if not possible:
        await _insert_and_deliver(
            db, event_ring, vapid_repo,
            account_id=account_id, message_id=message_id, folder_id=folder_id,
        )
    else:
        await _stage(db, account_id=account_id, message_id=message_id, folder_id=folder_id)


async def _insert_and_deliver(
    db: DatabaseConnection,
    event_ring: EventRing | None,
    vapid_repo: VapidKeyRepository | None,
    *,
    account_id: uuid.UUID,
    message_id: uuid.UUID,
    folder_id: uuid.UUID | None,
) -> None:
    facts = await _load_arrival_facts(db, account_id, message_id)
    if facts is None:
        return
    alert = await AlertRepository(db).create_mail_alert(
        account_id=account_id, message_id=message_id, msg_key=facts.msg_key,
        title=facts.title, body=facts.body, folder_id=folder_id, delivered=True,
    )
    if alert is None:
        return
    await _deliver(db, event_ring, vapid_repo, alert)


async def _stage(
    db: DatabaseConnection, *, account_id: uuid.UUID, message_id: uuid.UUID,
    folder_id: uuid.UUID | None,
) -> None:
    facts = await _load_arrival_facts(db, account_id, message_id)
    if facts is None:
        return
    await AlertRepository(db).create_mail_alert(
        account_id=account_id, message_id=message_id, msg_key=facts.msg_key,
        title=facts.title, body=facts.body, folder_id=folder_id, delivered=False,
    )


async def _deliver(
    db: DatabaseConnection,
    event_ring: EventRing | None,
    vapid_repo: VapidKeyRepository | None,
    alert: Alert,
) -> None:
    """Push alert.new over SSE and hand the alert to a background push
    dispatch -- the part of turning a row into a live notification that is
    the same whichever path (immediate or staged-then-finalized) produced
    a delivered row."""
    if vapid_repo is not None:
        # Fire-and-forget: _dispatch_push_safe never lets an exception
        # escape, so there is nothing for a caller to await.
        push_task = asyncio.create_task(
            _dispatch_push_safe(db, vapid_repo, alert, folder_id=alert.folder_id)
        )
        _background_push_tasks.add(push_task)
        push_task.add_done_callback(_background_push_tasks.discard)

    if event_ring is None or alert.account_id is None:
        return

    await event_ring.add(
        alert.account_id, "alert.new",
        {
            "id": str(alert.id), "kind": alert.kind, "title": alert.title,
            "body": alert.body, "url": alert.url, "account_id": str(alert.account_id),
            "folder_id": str(alert.folder_id) if alert.folder_id else None,
        },
    )


async def _dispatch_push_safe(
    db: DatabaseConnection,
    vapid_repo: VapidKeyRepository,
    alert: Alert,
    *,
    folder_id: uuid.UUID | None,
) -> None:
    """dispatch_push_for_alert already catches everything it expects to go
    wrong; this is the backstop for a background task, where an
    uncaught exception would otherwise only ever surface as an "exception
    was never retrieved" log line with no context."""
    try:
        await dispatch_push_for_alert(db, vapid_repo, alert, folder_id=folder_id)
    except Exception:
        logger.exception("Push dispatch failed for alert %s", alert.id)


async def _finalize_pending_mail_alerts_once(
    db: DatabaseConnection,
    event_ring: EventRing | None,
    vapid_repo: VapidKeyRepository | None,
    settings_service: SettingsService,
) -> None:
    """
    One tick: every staged mail alert whose message's pipeline run has
    reached a terminal status, whose message can no longer reach one
    (moved to a pipeline-excluded folder, or turned into a draft, before
    it did), or whose bound has expired, is delivered now with the folder
    the message is actually in by then. Everything else is left pending
    for the next tick.

    The pipeline_runs join is by message_id, kept in sync with a
    UIDVALIDITY resync the same way pipeline/enqueue.py's own
    reconciliation is (see enqueue_pipeline_run_if_live_eligible's
    docstring) -- but only for pipeline_runs' own message_id, not this
    staged alert's. A resync landing inside one alert's own staging
    window is rare and, worst case, just falls through to the bound
    rather than the terminal-status fast path; it is not lost.

    FOR UPDATE OF a SKIP LOCKED matters less here than in
    outbox/pending.py's twin (this pass already runs behind
    ReconciliationTimer's cross-connection advisory lock, so at most one
    replica ever reaches this query at a time) but costs nothing and
    keeps the two periodic dispatchers the same shape.
    """
    mail_settings = settings_service.get("mail") if settings_service.has_category("mail") else {}
    bound_seconds = float(mail_settings.get("notify_wait_seconds", 120.0))

    delivered_alerts: list[Alert] = []
    async with db.session() as session:
        candidates = (
            await session.execute(
                text(
                    """
                    SELECT a.id AS alert_id, m.id AS current_message_id,
                           m.folder_id AS current_folder_id, m.expunged_at, m.is_draft,
                           coalesce(fp.special_use_override, f.special_use, '')
                               AS effective_special_use,
                           pr.status AS pipeline_status,
                           (a.created_at <= now() - make_interval(secs => :bound_seconds))
                               AS bound_expired
                    FROM alerts a
                    LEFT JOIN messages m ON m.id = a.message_id AND m.account_id = a.account_id
                    LEFT JOIN folders f ON f.id = m.folder_id
                    LEFT JOIN folder_prefs fp ON fp.folder_id = f.id
                    LEFT JOIN pipeline_runs pr
                        ON pr.account_id = a.account_id AND pr.message_id = a.message_id
                        AND pr.dedup_key = 'live'
                    WHERE a.kind = 'mail' AND a.delivered_at IS NULL
                    ORDER BY a.created_at
                    LIMIT :batch
                    FOR UPDATE OF a SKIP LOCKED
                    """
                ),
                {"bound_seconds": bound_seconds, "batch": _FINALIZE_BATCH_SIZE},
            )
        ).all()
        if not candidates:
            return

        to_drop: list[uuid.UUID] = []
        to_deliver: list[tuple[uuid.UUID, uuid.UUID | None]] = []
        for row in candidates:
            if row.current_message_id is None or row.expunged_at is not None:
                # Gone before it was ever delivered -- an alert for mail
                # nobody can open any more is worse than no alert (the
                # same call the immediate path already makes).
                to_drop.append(row.alert_id)
                continue
            terminal = row.pipeline_status is not None and row.pipeline_status not in (
                "pending", "claimed",
            )
            never_eligible = row.is_draft or row.effective_special_use in _SKIP_FOLDER_SPECIAL_USE
            if terminal or never_eligible or row.bound_expired:
                to_deliver.append((row.alert_id, row.current_folder_id))

        for alert_id in to_drop:
            await session.execute(delete(Alert).where(Alert.id == alert_id))

        for alert_id, folder_id in to_deliver:
            result = await session.execute(
                update(Alert)
                .where(Alert.id == alert_id, Alert.delivered_at.is_(None))
                .values(folder_id=folder_id, delivered_at=text("now()"))
                .returning(Alert)
            )
            delivered = result.scalar_one_or_none()
            if delivered is not None:
                delivered_alerts.append(delivered)

    for alert in delivered_alerts:
        await _deliver(db, event_ring, vapid_repo, alert)

    if to_drop or delivered_alerts:
        logger.info(
            "Mail alert finalize pass",
            extra={"delivered": len(delivered_alerts), "dropped": len(to_drop)},
        )


def build_mail_alert_finalizer_timer(
    db: DatabaseConnection,
    event_ring: EventRing | None,
    vapid_repo: VapidKeyRepository | None,
    settings_service: SettingsService,
) -> ReconciliationTimer:
    """The advisory-locked periodic pass that delivers a staged mail
    alert once its message's pipeline run finishes or its bound
    expires -- one per process, safe with more than one replica."""

    async def _callback() -> None:
        await _finalize_pending_mail_alerts_once(db, event_ring, vapid_repo, settings_service)

    return ReconciliationTimer(db, _FINALIZE_LOCK_KEY, _callback, _FINALIZE_POLL_SECONDS)
