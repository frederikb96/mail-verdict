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
GET    /api/alerts/native-push              -- can a native app register here
POST   /api/alerts/subscriptions/native     -- register (or refresh) a native device
POST   /api/alerts/subscriptions/{id}/test  -- send one device a test push
POST   /api/alerts/lookup                   -- which of these alerts still exist
GET    /api/alerts/badge                    -- the notification badge for a device

An alert is something that interrupts the reader on their device -- new
mail today, a calendar reminder in a later feature. Every alert and every
device here is one user's (alerts/audience.py): each route reads, dismisses
and registers only the caller's own, and a device or alert of someone
else's named by id is refused (403). It reaches an open
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

from fastapi import APIRouter, Depends, HTTPException, Query

from mail_verdict.alerts.badge import badge_count
from mail_verdict.alerts.resolve import announce_alerts_dismissed
from mail_verdict.api.deps import current_scope, get_alert_repo, get_push_subscription_repo
from mail_verdict.api.events import get_event_ring
from mail_verdict.api.schemas import (
    AlertBadgeResponse,
    AlertLookupRequest,
    AlertResponse,
    AlertUnseenCountResponse,
    NativePushConfigResponse,
    NativeSubscriptionCreate,
    PushSubscriptionCreate,
    PushSubscriptionResponse,
    PushSubscriptionUpdate,
    VapidPublicKeyResponse,
)
from mail_verdict.auth.scope import Scope, ScopeDenied
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Alert, PushSubscription
from mail_verdict.push.relay import RelayOutcome, get_relay_client
from mail_verdict.push.send import PushUnavailableError, send_to_subscription
from mail_verdict.push.vapid import VapidUnavailableError, get_vapid_key_repo
from mail_verdict.settings.service import get_settings_service

# The kind a test push is sent as: not new mail, so it rides the system
# channel and shows its title as the banner title.
TEST_ALERT_KIND = "test"

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
    scope: Scope = Depends(current_scope),
) -> list[AlertResponse]:
    """The caller's durable alert list, newest first -- every account of
    theirs at once, the same as their SSE stream: an installed application
    watches every account from one page. "Which folders alert" is scoped the same way
    for this list as it already is for the SSE and push paths: the
    caller passes its own effective folder scope (use-push.ts's
    useEffectiveAlertFolderIds) rather than the server guessing at one."""
    repo = get_alert_repo()
    rows = await repo.list_recent(
        scope, limit=limit, folder_ids=folder_ids if folder_scoped else None,
        unseen_only=unseen_only,
    )
    return [AlertResponse.model_validate(row) for row in rows]


@router.get("/unseen-count", response_model=AlertUnseenCountResponse)
async def get_unseen_count(
    folder_ids: list[uuid.UUID] = Query(default=[]),
    folder_scoped: bool = Query(default=False),
    scope: Scope = Depends(current_scope),
) -> AlertUnseenCountResponse:
    """Delivered, not-yet-dismissed count, in total and per kind -- what
    the bell's badge is computed from, scoped the same way list_alerts is
    so the two never disagree."""
    repo = get_alert_repo()
    by_kind = await repo.unseen_counts_by_kind(
        scope, folder_ids=folder_ids if folder_scoped else None,
    )
    return AlertUnseenCountResponse(unseen=sum(by_kind.values()), by_kind=by_kind)


async def _announce_alerts_changed(alert_ids: list[uuid.UUID]) -> None:
    """Dismissing on one device or browser drops the same alert everywhere
    else it is still showing, cheaply, since SSE already reaches them all."""
    await announce_alerts_dismissed(get_db_connection(), get_event_ring(), alert_ids)


async def _own_subscription(scope: Scope, subscription_id: uuid.UUID) -> PushSubscription:
    """The caller's device by id: 404 if there is none, 403 if it is
    someone else's."""
    sub = await get_push_subscription_repo().get(subscription_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.user_id is None or sub.user_id != scope.user_id:
        raise ScopeDenied()
    return sub


@router.post("/{alert_id}/dismiss", status_code=204)
async def dismiss_alert(alert_id: uuid.UUID, scope: Scope = Depends(current_scope)) -> None:
    """Dismiss one of the caller's alerts. Idempotent -- see
    AlertRepository.dismiss; an alert that is not theirs is left alone."""
    repo = get_alert_repo()
    dismissed = await repo.dismiss(scope, alert_id)
    if dismissed:
        await _announce_alerts_changed([alert_id])


@router.post("/dismiss-all", status_code=204)
async def dismiss_all_alerts(
    kind: list[str] = Query(default=[]), scope: Scope = Depends(current_scope),
) -> None:
    """Dismiss every currently-undismissed alert of the caller's, or only
    those of the given kinds -- the bell dismisses its Mail and System tabs
    apart."""
    repo = get_alert_repo()
    dismissed = await repo.dismiss_all(scope, kinds=kind or None)
    if dismissed:
        await _announce_alerts_changed(dismissed)


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
async def list_push_subscriptions(
    scope: Scope = Depends(current_scope),
) -> list[PushSubscriptionResponse]:
    """The caller's registered devices -- the Settings page's own device list."""
    repo = get_push_subscription_repo()
    rows = await repo.list_for_user(scope.require_user())
    return [PushSubscriptionResponse.model_validate(row) for row in rows]


@router.post("/subscriptions", response_model=PushSubscriptionResponse, status_code=201)
async def register_push_subscription(
    body: PushSubscriptionCreate, scope: Scope = Depends(current_scope),
) -> PushSubscriptionResponse:
    """Register this device for the caller, or refresh it if `endpoint` is
    already registered (see PushSubscriptionRepository.upsert)."""
    repo = get_push_subscription_repo()
    row = await repo.upsert(
        scope.require_user(), endpoint=body.endpoint, p256dh=body.keys.p256dh, auth=body.keys.auth,
        label=body.label,
    )
    return PushSubscriptionResponse.model_validate(row)


@router.patch("/subscriptions/{subscription_id}", response_model=PushSubscriptionResponse)
async def update_push_subscription(
    subscription_id: uuid.UUID, body: PushSubscriptionUpdate,
    scope: Scope = Depends(current_scope),
) -> PushSubscriptionResponse:
    """Change one device's own alert preferences. A field the request
    body omits entirely is left untouched; `model_fields_set` is what
    tells that apart from a field explicitly sent as null (meaningful
    for alert_folder_ids -- null means "every folder")."""
    await _own_subscription(scope, subscription_id)
    repo = get_push_subscription_repo()
    fields = body.model_fields_set
    updated = await repo.update_prefs(
        subscription_id,
        alert_folder_ids=body.alert_folder_ids if "alert_folder_ids" in fields else "unset",
        reminders_enabled=body.reminders_enabled,
        label=body.label if "label" in fields else "unset",
        muted_channels=list(body.muted_channels) if body.muted_channels is not None else None,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return PushSubscriptionResponse.model_validate(updated)


@router.delete("/subscriptions/{subscription_id}", status_code=204)
async def delete_push_subscription(
    subscription_id: uuid.UUID, scope: Scope = Depends(current_scope),
) -> None:
    """Unregister one of the caller's devices -- the browser's own
    unsubscribe, or removing another device from the list. Idempotent: a
    device already gone is not an error."""
    repo = get_push_subscription_repo()
    sub = await repo.get(subscription_id)
    if sub is None:
        return
    if sub.user_id is None or sub.user_id != scope.user_id:
        raise ScopeDenied()
    await repo.delete(subscription_id)


@router.get("/native-push", response_model=NativePushConfigResponse)
async def get_native_push_config() -> NativePushConfigResponse:
    """Whether a native app can register for push here, and through which
    relays -- what the app checks before asking for notification
    permission."""
    relay = get_relay_client()
    reason = relay.unavailable_reason()
    return NativePushConfigResponse(
        available=reason is None, relay_urls=relay.relay_urls, reason=reason,
    )


@router.post("/subscriptions/native", response_model=PushSubscriptionResponse, status_code=201)
async def register_native_subscription(
    body: NativeSubscriptionCreate, scope: Scope = Depends(current_scope),
) -> PushSubscriptionResponse:
    """Register a native device for the caller, or refresh the one
    registered under this installation_id (see PushSubscriptionRepository.upsert_native). 400 if
    relay_url is not one push.apns_relay_urls allows, 503 if native push
    is unavailable here."""
    relay = get_relay_client()
    reason = relay.unavailable_reason()
    if reason is not None:
        raise HTTPException(status_code=503, detail=reason)
    if not relay.allows(body.relay_url):
        raise HTTPException(
            status_code=400,
            detail=f"relay_url is not a relay this server allows: {relay.relay_urls}",
        )
    encrypted_ticket, encrypted_key = relay.seal_credentials(
        body.ticket, body.content_key_bytes(),
    )
    row = await get_push_subscription_repo().upsert_native(
        scope.require_user(), installation_id=body.installation_id, relay_url=body.relay_url,
        encrypted_relay_ticket=encrypted_ticket, encrypted_content_key=encrypted_key,
        label=body.label,
        muted_channels=list(body.muted_channels) if body.muted_channels is not None else None,
    )
    return PushSubscriptionResponse.model_validate(row)


@router.post("/subscriptions/{subscription_id}/test", status_code=204)
async def send_test_push(
    subscription_id: uuid.UUID, scope: Scope = Depends(current_scope),
) -> None:
    """Send "Notifications are working" to one of the caller's devices
    through its own transport, without creating an alert. 404 if the device
    is unknown, 503 if its transport is unavailable here, 410 if the push
    service or relay says the device is gone (its row is removed), 502 if
    the send was refused for any other reason."""
    sub = await _own_subscription(scope, subscription_id)
    test_alert = Alert(id=uuid.uuid4(), kind=TEST_ALERT_KIND, title="Notifications are working")
    try:
        outcome = await send_to_subscription(
            get_db_connection(), get_vapid_key_repo(), get_relay_client(), test_alert, sub,
        )
    except PushUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if outcome is RelayOutcome.GONE:
        raise HTTPException(
            status_code=410, detail="The device can no longer be reached and was removed",
        )
    if outcome is RelayOutcome.FAILED:
        raise HTTPException(status_code=502, detail="The push was not accepted; try again later")


@router.post("/lookup", response_model=list[AlertResponse])
async def lookup_alerts(
    body: AlertLookupRequest, scope: Scope = Depends(current_scope),
) -> list[AlertResponse]:
    """The caller's alerts among these ids that still exist, dismissed or
    not -- a device drops a shown notification that is missing or
    dismissed here."""
    rows = await get_alert_repo().lookup(scope, body.ids)
    return [AlertResponse.model_validate(row) for row in rows]


@router.get("/badge", response_model=AlertBadgeResponse)
async def get_badge(
    subscription_id: uuid.UUID | None = Query(
        default=None, description="A registered device; its own folder scope applies",
    ),
    folder_ids: list[uuid.UUID] = Query(default=[]),
    folder_scoped: bool = Query(
        default=False,
        description="Apply folder_ids -- as on GET /alerts. Ignored with subscription_id.",
    ),
    scope: Scope = Depends(current_scope),
) -> AlertBadgeResponse:
    """The caller's notification badge -- alerts/badge.py decides what it
    counts. A subscription_id must be one of the caller's devices."""
    db = get_db_connection()
    if subscription_id is not None:
        sub = await _own_subscription(scope, subscription_id)
        count = await badge_count(db, get_settings_service(), scope, subscription=sub)
    else:
        count = await badge_count(
            db, get_settings_service(), scope, folder_ids=folder_ids if folder_scoped else None,
        )
    return AlertBadgeResponse(count=count)
