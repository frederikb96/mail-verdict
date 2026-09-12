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

The alert resolves itself once the message stops waiting on the user: its
outbox row is sent, or its staged send is cancelled. A dead row keeps it,
since the message still never went out. Each pass resolves first; a sent
outbox event, and Undo on a staged send (api/outbox.py), each resolve their
own row's alert at once.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import ColumnElement, Text, cast, exists, func, select, text

from mail_verdict.alerts.dispatch import deliver_alert
from mail_verdict.alerts.resolve import announce_alerts_dismissed
from mail_verdict.database.models import Account, Alert, Outbox, PendingSend, SyncState
from mail_verdict.database.repository import AlertRepository
from mail_verdict.queue.notify import ReconciliationTimer

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

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
    # The account's own health, None where the account row is gone.
    is_active: bool | None
    state: str | None
    last_full_sync: datetime | None


def _not_alerted(row_id: Any) -> ColumnElement[bool]:
    return ~exists().where(Alert.dedupe_key == func.concat(DEDUPE_PREFIX, cast(row_id, Text)))


_ACCOUNT_HEALTH = (Account.is_active, Account.state, SyncState.last_full_sync)


async def _find_stalled(db: DatabaseConnection, threshold: timedelta) -> list[_Stalled]:
    """Outbox rows PostIMAP has not finished with, and staged sends past
    their window, waiting longer than `threshold` and not yet alerted --
    each with its account's health, which is usually why it is waiting."""
    cutoff = func.now() - threshold
    async with db.session() as session:
        queued = await session.execute(
            select(
                Outbox.id, Outbox.account_id, Outbox.kind, Outbox.subject, Outbox.created_at,
                *_ACCOUNT_HEALTH,
            )
            .outerjoin(Account, Account.id == Outbox.account_id)
            .outerjoin(SyncState, SyncState.account_id == Outbox.account_id)
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
                PendingSend.send_after, *_ACCOUNT_HEALTH,
            )
            .outerjoin(Account, Account.id == PendingSend.account_id)
            .outerjoin(SyncState, SyncState.account_id == PendingSend.account_id)
            .where(
                PendingSend.cancelled_at.is_(None),
                PendingSend.send_after < cutoff,
                _not_alerted(PendingSend.id),
            )
            .limit(_BATCH_SIZE)
        )
        return [
            *(
                _Stalled(
                    r.id, r.account_id, r.kind, r.subject, r.created_at,
                    r.is_active, r.state, r.last_full_sync,
                )
                for r in queued
            ),
            *(
                _Stalled(
                    r.id, r.account_id, "send", r.subject, r.send_after,
                    r.is_active, r.state, r.last_full_sync,
                )
                for r in staged
            ),
        ]


def _connection_note(row: _Stalled) -> str | None:
    """Why the account cannot send right now, when that is the case --
    PostIMAP holds an outbox row pending while its account has no
    connection. Follows the consumer contract's reading of account health:
    `error` is retried without end, and last_full_sync separates an account
    that has worked before from one that has never connected."""
    if row.is_active is False:
        return "the account is paused, and it goes out once the account is resumed"
    if row.state == "disabled":
        return "the account is disabled, so nothing is sent from it"
    if row.state == "error":
        if row.last_full_sync is None:
            return "the account has never connected -- check its server settings"
        return "the account is disconnected and being retried, and it goes out once it reconnects"
    return None


def _describe(row: _Stalled) -> tuple[str, str]:
    """Title and body -- rendered once here, the same way a mail alert's
    are, rather than by every reader of the list."""
    minutes = int((datetime.now(timezone.utc) - row.waiting_since).total_seconds() // 60)
    title = "Draft not saved yet" if row.kind == "draft" else "Message not sent yet"
    body = f"{row.subject or '(no subject)'} -- still waiting after {minutes} min"
    note = _connection_note(row)
    return title, f"{body}: {note}" if note else body


def _settled_sql(scope: str) -> str:
    """Resolve every unresolved stalled alert within `scope` whose row was
    sent, or whose staged send was cancelled. The row id is the part of
    dedupe_key after DEDUPE_PREFIX."""
    row_id = "substr(a.dedupe_key, :prefix_len + 1)::uuid"
    return f"""
        UPDATE alerts a SET dismissed_at = now()
        WHERE a.kind = 'outbox_stalled'
          AND a.dismissed_at IS NULL
          AND ({scope})
          AND (
            EXISTS (SELECT 1 FROM outbox o WHERE o.id = {row_id} AND o.status = 'sent')
            OR EXISTS (
                SELECT 1 FROM pending_sends p
                WHERE p.id = {row_id} AND p.cancelled_at IS NOT NULL
            )
          )
        RETURNING a.id
    """


async def resolve_settled_stalled_alerts(
    session: AsyncSession, outbox_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """
    Resolve stalled alerts whose message no longer waits on the user.

    Args:
        session: Active AsyncSession (caller commits)
        outbox_id: Only this row's alert; None considers every one

    Returns:
        The ids of the alerts this call resolved
    """
    params: dict[str, Any] = {"prefix_len": len(DEDUPE_PREFIX)}
    scope = "true"
    if outbox_id is not None:
        scope = "a.dedupe_key = :dedupe_key"
        params["dedupe_key"] = f"{DEDUPE_PREFIX}{outbox_id}"
    result = await session.execute(text(_settled_sql(scope)), params)
    return list(result.scalars().all())


async def resolve_stalled_for_outbox_event(
    db: DatabaseConnection, event_ring: EventRing | None, outbox_id: str,
) -> list[uuid.UUID]:
    """The postimap event path: an outbox row changed status -- resolve its
    stalled alert if that settled it, and announce it."""
    try:
        outbox_uuid = uuid.UUID(outbox_id)
    except ValueError:
        return []
    async with db.session() as session:
        resolved = await resolve_settled_stalled_alerts(session, outbox_uuid)
    if resolved:
        await announce_alerts_dismissed(db, event_ring)
    return resolved


async def raise_stalled_outbox_alerts_once(
    db: DatabaseConnection,
    event_ring: EventRing | None,
    vapid_repo: VapidKeyRepository | None,
    *,
    threshold: timedelta,
) -> None:
    """One pass: resolve the alerts whose message has since gone out or
    been cancelled, then alert, once each, for every message waiting
    longer than `threshold` on its way out."""
    async with db.session() as session:
        resolved = await resolve_settled_stalled_alerts(session)
    if resolved:
        await announce_alerts_dismissed(db, event_ring)

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
