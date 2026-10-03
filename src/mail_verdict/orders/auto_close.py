"""
The automatic close: an open order whose open/closed state still belongs
to the model is closed once nothing is expected of it any more.

Two rules, by whether the write call estimated an end date
(orders.expected_until -- an event, a trip's last day, a pickup deadline,
about a week after a parcel shipped):
- with a date: orders.auto_close_grace_days after the later of that date
  and the last mail;
- without one: orders.auto_close_days after the last mail.
orders.auto_close_days = 0 turns the whole sweep off; a grace of 0 closes
as soon as the date has passed.

Skipped: an order a person opened or closed (open_set_by <> 'ai' -- a
reopen stays open until the next mail hands the decision back to the
model), an order never written, and an order whose text is stale (its
rewrite is queued and may still change expected_until).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import text

from mail_verdict.api.events import broadcast_event
from mail_verdict.orders.repository import OPEN_SET_BY_AUTO, OPEN_SET_BY_MODEL
from mail_verdict.orders.worker import ORDERS_LOCK_KEY
from mail_verdict.queue.notify import ReconciliationTimer

if TYPE_CHECKING:
    import uuid

    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.settings.service import SettingsService

logger = logging.getLogger(__name__)

# test_lock_keys.py asserts every *_LOCK_KEY constant in the package is
# pairwise distinct.
_CLOSE_LOCK_KEY = 761_035_300

# A closing that is a day late costs nothing, so hourly is plenty.
_CLOSE_INTERVAL_SECONDS = 3600.0


async def close_overdue_orders(
    db: DatabaseConnection, *, days: int, grace_days: int,
) -> list[uuid.UUID]:
    """
    Close every open, model-owned, written, non-stale order that is overdue
    by the rules in this module's docstring.

    Returns:
        The ids of the orders closed
    """
    async with db.session() as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ORDERS_LOCK_KEY})
        result = await session.execute(
            text(
                """
                UPDATE orders
                SET is_open = false, open_set_by = :auto, updated_at = now()
                WHERE is_open
                  AND open_set_by = :model
                  AND written_at IS NOT NULL
                  AND NOT text_stale
                  AND last_mail_at IS NOT NULL
                  AND CASE
                        WHEN expected_until IS NULL
                          THEN last_mail_at < now() - make_interval(days => :days)
                        ELSE greatest(last_mail_at, expected_until::timestamptz)
                          < now() - make_interval(days => :grace)
                      END
                RETURNING id
                """
            ),
            {
                "auto": OPEN_SET_BY_AUTO, "model": OPEN_SET_BY_MODEL,
                "days": days, "grace": grace_days,
            },
        )
        return [row[0] for row in result.all()]


async def auto_close_once(
    db: DatabaseConnection, settings_service: SettingsService, event_ring: EventRing | None,
) -> None:
    """One pass: close what is overdue under the current setting (nothing
    when it is 0) and announce each closed order."""
    settings = settings_service.get("orders")
    days = int(settings["auto_close_days"])
    if days <= 0:
        return
    closed = await close_overdue_orders(
        db, days=days, grace_days=int(settings["auto_close_grace_days"]),
    )
    if not closed:
        return
    logger.info("Closed %d overdue orders", len(closed))
    if event_ring is not None:
        for order_id in closed:
            await broadcast_event(
                db, event_ring, "order.updated",
                {"order_id": str(order_id), "change": "updated"},
            )


def build_auto_close_timer(
    db: DatabaseConnection, settings_service: SettingsService, event_ring: EventRing | None,
) -> ReconciliationTimer:
    """The advisory-locked hourly pass that closes overdue orders -- one
    per process, safe with more than one replica."""

    async def _callback() -> None:
        await auto_close_once(db, settings_service, event_ring)

    return ReconciliationTimer(db, _CLOSE_LOCK_KEY, _callback, _CLOSE_INTERVAL_SECONDS)
