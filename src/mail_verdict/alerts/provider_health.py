"""
Raising an alert for an AI provider that stays unavailable.

A breaker that suspends (no key, a rejected key, a spent credit balance or
token allowance -- pipeline/context.py) holds its queue until the provider
answers again, and says so only in the queue API and in
GET /api/settings/providers. Nobody looks there on an ordinary day, so a
topped-up balance that never happens would go unnoticed for weeks while
orders stay stale and mail goes unjudged.

This pass looks at the breakers of the AI tasks and alerts, once per
outage, for every one that has been suspended longer than the configured
threshold. The alert's dedupe_key names the breaker and the instant it was
suspended, so the same outage is never announced twice and a later one is.
It resolves itself -- dismissed, like a click on it -- once that breaker is
no longer suspended.

The alert is an ordinary system-channel alert (a kind the apps already
list and push like any other that is not new mail); it belongs to no
account.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import or_, select, update

from mail_verdict.alerts.dispatch import deliver_alert
from mail_verdict.alerts.resolve import announce_alerts_dismissed
from mail_verdict.database.models import Alert, CircuitBreakerState
from mail_verdict.database.repository import AlertRepository
from mail_verdict.queue.notify import ReconciliationTimer
from mail_verdict.settings.ai_validation import AI_TASK_CATEGORIES

if TYPE_CHECKING:
    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.push.vapid import VapidKeyRepository

logger = logging.getLogger(__name__)

# Distinct from every other ReconciliationTimer's lock key in the process
# (tests/unit/test_lock_keys.py checks them all).
_PROVIDER_ALERT_LOCK_KEY = 761_035_500

_POLL_INTERVAL_SECONDS = 60.0

ALERT_KIND = "provider_unavailable"
DEDUPE_PREFIX = "provider-suspended:"


def _dedupe_key(name: str, since: datetime) -> str:
    return f"{DEDUPE_PREFIX}{name}:{int(since.timestamp())}"


async def _suspended(db: DatabaseConnection) -> list[tuple[str, str | None, datetime]]:
    """(name, reason, since) of every AI task breaker that is suspended."""
    suffixes = [CircuitBreakerState.name.like(f"%:{task}") for task in AI_TASK_CATEGORIES]
    async with db.session() as session:
        rows = (
            await session.execute(
                select(
                    CircuitBreakerState.name, CircuitBreakerState.reason,
                    CircuitBreakerState.since,
                ).where(
                    CircuitBreakerState.state == "suspended",
                    CircuitBreakerState.since.is_not(None),
                    or_(*suffixes),
                )
            )
        ).all()
    return [(row.name, row.reason, row.since) for row in rows]


async def _resolve_recovered(
    db: DatabaseConnection, suspended: list[tuple[str, str | None, datetime]],
) -> bool:
    """Dismiss the open provider alerts whose outage is over. Returns whether
    any alert was dismissed."""
    live = {_dedupe_key(name, since) for name, _, since in suspended}
    async with db.session() as session:
        open_alerts = (
            await session.execute(
                select(Alert.id, Alert.dedupe_key).where(
                    Alert.kind == ALERT_KIND, Alert.dismissed_at.is_(None),
                )
            )
        ).all()
        recovered = [row.id for row in open_alerts if row.dedupe_key not in live]
        if recovered:
            await session.execute(
                update(Alert).where(Alert.id.in_(recovered)).values(
                    dismissed_at=datetime.now(timezone.utc),
                )
            )
    return bool(recovered)


async def raise_provider_alerts_once(
    db: DatabaseConnection,
    event_ring: EventRing | None,
    vapid_repo: VapidKeyRepository | None,
    *,
    threshold: timedelta,
) -> None:
    """One pass: resolve the alerts of outages that ended, then alert, once
    each, for every breaker suspended longer than `threshold`."""
    suspended = await _suspended(db)
    if await _resolve_recovered(db, suspended):
        await announce_alerts_dismissed(db, event_ring)

    now = datetime.now(timezone.utc)
    repo = AlertRepository(db)
    for name, reason, since in suspended:
        if now - since < threshold:
            continue
        minutes = int((now - since).total_seconds() // 60)
        alert = await repo.create_provider_unavailable_alert(
            dedupe_key=_dedupe_key(name, since),
            title="AI provider unavailable",
            body=f"{name} has been suspended for {minutes} min: {reason or 'no reason recorded'}",
        )
        if alert is None:
            continue
        logger.warning("AI provider suspended", extra={"breaker": name, "minutes": minutes})
        await deliver_alert(db, event_ring, vapid_repo, alert)


def build_provider_alert_timer(
    db: DatabaseConnection,
    event_ring: EventRing | None,
    vapid_repo: VapidKeyRepository | None,
    threshold: timedelta,
) -> ReconciliationTimer:
    """The advisory-locked periodic pass that raises provider alerts -- one
    per process, safe with more than one replica."""

    async def _callback() -> None:
        await raise_provider_alerts_once(db, event_ring, vapid_repo, threshold=threshold)

    return ReconciliationTimer(db, _PROVIDER_ALERT_LOCK_KEY, _callback, _POLL_INTERVAL_SECONDS)
