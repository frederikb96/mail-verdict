"""
Alert centre API.

GET    /api/alerts                          -- durable list, newest first
GET    /api/alerts/unseen-count             -- badge count
POST   /api/alerts/{id}/dismiss             -- dismiss one
POST   /api/alerts/dismiss-all              -- dismiss every undismissed alert
GET    /api/alerts/vapid-public-key         -- this server's Web Push signing key
GET    /api/alerts/subscriptions            -- every registered device
POST   /api/alerts/subscriptions            -- register (or refresh) this device
PATCH  /api/alerts/subscriptions/{id}       -- change one device's own preferences
DELETE /api/alerts/subscriptions/{id}       -- unregister a device

An alert is something that interrupts the reader on their device -- new
mail today, a calendar reminder in a later feature. It reaches an open
page over the in-app path (reacting to alert.new on the SSE stream,
needing none of what follows) and, for a registered device, over Web
Push as well -- reaching it even with no MailVerdict page open, which is
what the rest of this module is for. The row insert and the push
dispatch both live in alerts/dispatch.py and push/send.py, called from
server.py's postimap event handler next to the mail.new SSE event they
ride alongside; this module is the read/acknowledge surface plus the
subscription lifecycle.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query

from mail_verdict.alerts.resolve import announce_alerts_dismissed
from mail_verdict.api.deps import get_alert_repo, get_push_subscription_repo
from mail_verdict.api.events import get_event_ring
from mail_verdict.api.schemas import (
    AlertResponse,
    AlertUnseenCountResponse,
    PushSubscriptionCreate,
    PushSubscriptionResponse,
    PushSubscriptionUpdate,
    VapidPublicKeyResponse,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.push.vapid import VapidUnavailableError, get_vapid_key_repo

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_model=list[AlertResponse])
async def list_alerts(
    limit: int = Query(default=50, ge=1, le=200),
    folder_ids: list[uuid.UUID] = Query(default=[]),
    folder_scoped: bool = Query(
        default=False,
        description=(
            "Whether folder_ids should be applied at all -- an omitted or "
            "empty folder_ids is ambiguous over a query string (both look "
            "the same as not sending the parameter), so this is what "
            "actually distinguishes 'no restriction' from 'restricted to "
            "nothing'."
        ),
    ),
    unseen_only: bool = Query(default=False),
) -> list[AlertResponse]:
    """The durable alert list, newest first -- not account-scoped, the
    same as the SSE stream itself: an installed application watches every
    account from one page. "Which folders alert" is scoped the same way
    for this list as it already is for the SSE and push paths: the
    caller passes its own effective folder scope (use-push.ts's
    useEffectiveAlertFolderIds) rather than the server guessing at one."""
    repo = get_alert_repo()
    rows = await repo.list_recent(
        limit=limit, folder_ids=folder_ids if folder_scoped else None,
        unseen_only=unseen_only,
    )
    return [AlertResponse.model_validate(row) for row in rows]


@router.get("/unseen-count", response_model=AlertUnseenCountResponse)
async def get_unseen_count(
    folder_ids: list[uuid.UUID] = Query(default=[]),
    folder_scoped: bool = Query(default=False),
) -> AlertUnseenCountResponse:
    """Delivered, not-yet-dismissed count -- the bell's own badge, scoped
    the same way list_alerts is so the two never disagree."""
    repo = get_alert_repo()
    count = await repo.unseen_count(folder_ids=folder_ids if folder_scoped else None)
    return AlertUnseenCountResponse(unseen=count)


async def _announce_alerts_changed() -> None:
    """Dismissing on one device or browser drops the same alert everywhere
    else it is still showing, cheaply, since SSE already reaches them all."""
    await announce_alerts_dismissed(get_db_connection(), get_event_ring())


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


@router.get("/vapid-public-key", response_model=VapidPublicKeyResponse)
async def get_vapid_public_key() -> VapidPublicKeyResponse:
    """The key a browser needs to call `PushManager.subscribe()`.
    Generates the server's keypair the first time this is ever called --
    see push/vapid.py. `available: false` (rather than an error) is the
    entire "push is off" signal a deployment with no ENCRYPTION_KEY
    configured needs, since a browser asks this before it ever tries to
    subscribe."""
    try:
        public_key = await get_vapid_key_repo().public_key_b64()
    except VapidUnavailableError:
        return VapidPublicKeyResponse(available=False, public_key=None)
    return VapidPublicKeyResponse(available=True, public_key=public_key)


@router.get("/subscriptions", response_model=list[PushSubscriptionResponse])
async def list_push_subscriptions() -> list[PushSubscriptionResponse]:
    """Every registered device -- the Settings page's own device list."""
    repo = get_push_subscription_repo()
    rows = await repo.list_all()
    return [PushSubscriptionResponse.model_validate(row) for row in rows]


@router.post("/subscriptions", response_model=PushSubscriptionResponse, status_code=201)
async def register_push_subscription(
    body: PushSubscriptionCreate,
) -> PushSubscriptionResponse:
    """Register this device, or refresh it if `endpoint` is already
    registered (see PushSubscriptionRepository.upsert)."""
    repo = get_push_subscription_repo()
    row = await repo.upsert(
        endpoint=body.endpoint, p256dh=body.keys.p256dh, auth=body.keys.auth,
        label=body.label,
    )
    return PushSubscriptionResponse.model_validate(row)


@router.patch("/subscriptions/{subscription_id}", response_model=PushSubscriptionResponse)
async def update_push_subscription(
    subscription_id: uuid.UUID, body: PushSubscriptionUpdate,
) -> PushSubscriptionResponse:
    """Change one device's own alert preferences. A field the request
    body omits entirely is left untouched; `model_fields_set` is what
    tells that apart from a field explicitly sent as null (meaningful
    for alert_folder_ids -- null means "every folder")."""
    repo = get_push_subscription_repo()
    fields = body.model_fields_set
    updated = await repo.update_prefs(
        subscription_id,
        alert_folder_ids=body.alert_folder_ids if "alert_folder_ids" in fields else "unset",
        reminders_enabled=body.reminders_enabled,
        label=body.label if "label" in fields else "unset",
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return PushSubscriptionResponse.model_validate(updated)


@router.delete("/subscriptions/{subscription_id}", status_code=204)
async def delete_push_subscription(subscription_id: uuid.UUID) -> None:
    """Unregister a device -- the browser's own unsubscribe, or removing
    another device from the list."""
    await get_push_subscription_repo().delete(subscription_id)
