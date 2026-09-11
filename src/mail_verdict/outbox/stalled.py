"""
Raising an alert for a message that sits on its way out far longer than it
should.

Nothing else says so. PostIMAP reports a write that gives up -- a dead
send lands in sync_notifications -- but a row it never picks up at all (an
outbox processor that stopped without failing) produces no failure to
report, and neither does a staged send the undo-window worker could not
move into outbox (outbox/pending.py leaves such a row where it is). Both
simply wait. This pass looks for exactly that: an outbox row still pending
or processing, or an uncancelled staged send past its window, for longer
than config.outbox.stalled_alert_after_seconds. A failed row is not
included -- PostIMAP is retrying it, and dead-letters it with a
notification of its own when it stops.

Once per message: the alert's dedupe_key is the row's own id and the
insert is ON CONFLICT DO NOTHING, the same fires-exactly-once mechanism a
mail alert uses, so a row still stuck on the next pass -- or one the user
already dismissed -- raises nothing new. A staged send keeps its id when
it moves into outbox, so both sources share one key space and a send
stuck first in one and then in the other still alerts once.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import ColumnElement, Text, cast, exists, func, select

from mail_verdict.alerts.dispatch import deliver_alert
from mail_verdict.database.models import Alert, Outbox, PendingSend
from mail_verdict.database.repository import AlertRepository
from mail_verdict.queue.notify import ReconciliationTimer

if TYPE_CHECKING:
    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.push.vapid import VapidKeyRepository

logger = logging.getLogger(__name__)

# Distinct from every other ReconciliationTimer's lock key in the process
# (tests/unit/test_lock_keys.py checks them all).
_STALLED_LOCK_KEY = 761_034_900

# The threshold is minutes; checking once a minute costs one indexed-free
# scan of two small tables and adds at most a minute to it.
_POLL_INTERVAL_SECONDS = 60.0

_BATCH_SIZE = 50

DEDUPE_PREFIX = "outbox-stalled:"


class _Stalled(NamedTuple):
    id: uuid.UUID
    account_id: uuid.UUID
    kind: str
    subject: str | None
    waiting_since: datetime


def _not_alerted(row_id: Any) -> ColumnElement[bool]:
    return ~exists().where(Alert.dedupe_key == func.concat(DEDUPE_PREFIX, cast(row_id, Text)))


async def _find_stalled(db: DatabaseConnection, threshold: timedelta) -> list[_Stalled]:
    """Outbox rows PostIMAP has not finished with, and staged sends past
    their window, waiting longer than `threshold` and not yet alerted."""
    cutoff = func.now() - threshold
    async with db.session() as session:
        queued = await session.execute(
            select(Outbox.id, Outbox.account_id, Outbox.kind, Outbox.subject, Outbox.created_at)
            .where(
                Outbox.status.in_(("pending", "processing")),
                Outbox.created_at < cutoff,
                _not_alerted(Outbox.id),
            )
            .limit(_BATCH_SIZE)
        )
        staged = await session.execute(
            select(
                PendingSend.id, PendingSend.account_id, PendingSend.subject,
                PendingSend.send_after,
            )
            .where(
                PendingSend.cancelled_at.is_(None),
                PendingSend.send_after < cutoff,
                _not_alerted(PendingSend.id),
            )
            .limit(_BATCH_SIZE)
        )
        return [
            *(_Stalled(r.id, r.account_id, r.kind, r.subject, r.created_at) for r in queued),
            *(_Stalled(r.id, r.account_id, "send", r.subject, r.send_after) for r in staged),
        ]


def _describe(row: _Stalled) -> tuple[str, str]:
    """Title and body -- rendered once here, the same way a mail alert's
    are, rather than by every reader of the list."""
    minutes = int((datetime.now(timezone.utc) - row.waiting_since).total_seconds() // 60)
    title = "Draft not saved yet" if row.kind == "draft" else "Message not sent yet"
    return title, f"{row.subject or '(no subject)'} -- still waiting after {minutes} min"


async def raise_stalled_outbox_alerts_once(
    db: DatabaseConnection,
    event_ring: EventRing | None,
    vapid_repo: VapidKeyRepository | None,
    *,
    threshold: timedelta,
) -> None:
    """One pass: alert, once each, for every message waiting longer than
    `threshold` on its way out."""
    repo = AlertRepository(db)
    for row in await _find_stalled(db, threshold):
        title, body = _describe(row)
        alert = await repo.create_outbox_stalled_alert(
            account_id=row.account_id, dedupe_key=f"{DEDUPE_PREFIX}{row.id}",
            title=title, body=body,
        )
        if alert is None:
            continue
        logger.warning(
            "Outbox row stalled", extra={"outbox_id": str(row.id), "kind": row.kind},
        )
        await deliver_alert(db, event_ring, vapid_repo, alert)


def build_stalled_outbox_timer(
    db: DatabaseConnection,
    event_ring: EventRing | None,
    vapid_repo: VapidKeyRepository | None,
    threshold: timedelta,
) -> ReconciliationTimer:
    """The advisory-locked periodic pass that raises stalled-outbox
    alerts -- one per process, safe with more than one replica."""

    async def _callback() -> None:
        await raise_stalled_outbox_alerts_once(db, event_ring, vapid_repo, threshold=threshold)

    return ReconciliationTimer(db, _STALLED_LOCK_KEY, _callback, _POLL_INTERVAL_SECONDS)
