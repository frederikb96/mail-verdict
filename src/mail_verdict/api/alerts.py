"""
Alert centre API.

GET  /api/alerts                 -- durable list, newest first
GET  /api/alerts/unseen-count    -- badge count
POST /api/alerts/{id}/dismiss    -- dismiss one
POST /api/alerts/dismiss-all     -- dismiss every undismissed alert

An alert is something that interrupts the reader on their device -- new
mail today, a calendar reminder in a later feature -- delivered here over
the in-app path (an open page reacting to alert.new on the SSE stream) so
it needs no push subscription, no VAPID keys and no service worker. The
row insert that produces one lives in server.py's postimap event handler,
next to the mail.new SSE event it rides alongside; this module is only
the read/acknowledge surface.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from mail_verdict.api.deps import get_alert_repo
from mail_verdict.api.events import broadcast_event, get_event_ring
from mail_verdict.api.schemas import AlertResponse, AlertUnseenCountResponse
from mail_verdict.database.connection import get_db_connection

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_model=list[AlertResponse])
async def list_alerts(limit: int = Query(default=50, ge=1, le=200)) -> list[AlertResponse]:
    """The durable alert list, newest first -- not account-scoped, the
    same as the SSE stream itself: an installed application watches every
    account from one page, and "which folders alert" is a narrower,
    per-browser choice made client-side, not a server-side filter here."""
    repo = get_alert_repo()
    rows = await repo.list_recent(limit=limit)
    return [AlertResponse.model_validate(row) for row in rows]


@router.get("/unseen-count", response_model=AlertUnseenCountResponse)
async def get_unseen_count() -> AlertUnseenCountResponse:
    """Delivered, not-yet-dismissed count -- the bell's own badge."""
    repo = get_alert_repo()
    count = await repo.unseen_count()
    return AlertUnseenCountResponse(unseen=count)


async def _announce_alerts_changed() -> None:
    """alert.dismissed reaches every open page over the SSE ring it
    already holds -- so dismissing on one device or browser drops the
    same alert everywhere else it is still showing, cheaply, since SSE
    already reaches them all."""
    event_ring = get_event_ring()
    if event_ring is not None:
        await broadcast_event(get_db_connection(), event_ring, "alert.dismissed", {})


@router.post("/{alert_id}/dismiss", status_code=204)
async def dismiss_alert(alert_id: uuid.UUID) -> None:
    """Dismiss one alert. Idempotent -- see AlertRepository.dismiss."""
    repo = get_alert_repo()
    dismissed = await repo.dismiss(alert_id)
    if dismissed:
        await _announce_alerts_changed()


@router.post("/dismiss-all", status_code=204)
async def dismiss_all_alerts() -> None:
    """Dismiss every currently-undismissed alert."""
    repo = get_alert_repo()
    count = await repo.dismiss_all()
    if count > 0:
        await _announce_alerts_changed()
