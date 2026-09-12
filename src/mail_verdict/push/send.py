"""
Sending an alert to every push subscription that wants it.

Generic over alert kind on purpose -- a mail alert (delivered immediately,
folder-scoped), a stuck-send alert, and a future reminder alert all end
here, so there is one place that knows about subscriptions and one cleanup
rule per transport: a subscription its push service or relay says will
never accept another push is deleted, anything else is stamped failed_at
and left for the next alert.

Two transports. A browser ("webpush") is sent the alert over Web Push,
signed with this server's VAPID key (push/vapid.py): a 404 or 410 from the
push service is its own protocol-level unsubscribe signal (RFC 8030 s7).
A native app ("apns") is sent an envelope sealed with its own content key
(push/envelope.py) through its push relay (push/relay.py), carrying its
own badge (alerts/badge.py) and the mail alerts recently resolved, so the
phone can withdraw those banners. Each transport's credential is fetched
only when a row of that transport is about to be sent to.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

from pywebpush import WebPushException, webpush_async

from mail_verdict.alerts.badge import badge_count
from mail_verdict.core.encryption import EncryptionError
from mail_verdict.database.repository import AlertRepository, PushSubscriptionRepository
from mail_verdict.push.envelope import MAX_RESOLVED_IDS, alert_payload, seal_payload
from mail_verdict.push.relay import RelayOutcome
from mail_verdict.push.vapid import VapidUnavailableError
from mail_verdict.settings.service import get_settings_service

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.database.models import Alert, PushSubscription
    from mail_verdict.push.relay import RelayClient
    from mail_verdict.push.vapid import VapidKeyRepository

logger = logging.getLogger(__name__)

# VAPID's `sub` claim is informational to the push service (who to
# contact about abuse), never seen by a subscriber and not part of the
# cryptography -- a fixed, non-identifying value avoids this needing a
# deployment-specific setting.
_VAPID_SUBJECT = "mailto:push@localhost"

# An alert is time-sensitive: a push service still retrying delivery five
# minutes from now is not useful the way the in-app alert, already
# delivered before this runs, was immediately.
_TTL_SECONDS = 300

# How far back a native push reaches for mail alerts resolved elsewhere.
_RESOLVED_WITHIN = timedelta(hours=24)


class PushUnavailableError(Exception):
    """A subscription's transport cannot send from this server: no
    ENCRYPTION_KEY, native push disabled, or its relay no longer allowed."""


async def dispatch_push_for_alert(
    db: DatabaseConnection,
    vapid_repo: VapidKeyRepository,
    alert: Alert,
    *,
    folder_id: uuid.UUID | None,
    relay: RelayClient | None,
) -> None:
    """
    Push `alert` to every subscription eligible for it.

    Never raises: an unconfigured ENCRYPTION_KEY or a subscriber's own
    outage must never affect the alert row or the in-app SSE path, both
    already delivered by the caller before this is reached.

    Args:
        db: Database connection
        vapid_repo: This server's VAPID identity, for browser rows
        alert: The alert to send -- already inserted and, for a "mail"
            alert, already delivered_at-stamped
        folder_id: The message's folder, threaded through rather than
            re-queried, for a "mail" alert's per-subscription folder
            filter. Ignored for other kinds.
        relay: The relay client for native rows; None skips them
    """
    repo = PushSubscriptionRepository(db)
    subscriptions = await repo.list_for_alert(kind=alert.kind, folder_id=folder_id)
    if not subscriptions:
        return

    resolved: list[uuid.UUID] = []
    if any(sub.transport == "apns" for sub in subscriptions):
        resolved = await AlertRepository(db).recently_resolved_mail(
            limit=MAX_RESOLVED_IDS, within=_RESOLVED_WITHIN,
        )

    results = await asyncio.gather(
        *(
            send_to_subscription(db, vapid_repo, relay, alert, sub, resolved=resolved)
            for sub in subscriptions
        ),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, PushUnavailableError):
            continue
        if isinstance(result, BaseException):
            logger.warning("Unexpected error sending push to a subscription: %s", result)


async def send_to_subscription(
    db: DatabaseConnection,
    vapid_repo: VapidKeyRepository,
    relay: RelayClient | None,
    alert: Alert,
    sub: PushSubscription,
    *,
    resolved: list[uuid.UUID],
) -> RelayOutcome:
    """
    Send one alert to one subscription and record the outcome on its row.

    Args:
        db: Database connection
        vapid_repo: This server's VAPID identity, for a browser row
        relay: The relay client, for a native row
        alert: What to send -- need not be a stored row
        sub: The subscription
        resolved: Mail alerts a native device should withdraw

    Returns:
        The outcome, already applied to the row

    Raises:
        PushUnavailableError: This row's transport cannot send from here
    """
    repo = PushSubscriptionRepository(db)
    if sub.transport == "apns":
        outcome = await _send_native(db, relay, alert, sub, resolved=resolved)
    else:
        outcome = await _send_webpush(vapid_repo, alert, sub)

    if outcome is RelayOutcome.DELIVERED:
        await repo.mark_seen(sub.id)
    elif outcome is RelayOutcome.GONE:
        await repo.delete(sub.id)
    else:
        await repo.mark_failed(sub.id)
    return outcome


async def _send_webpush(
    vapid_repo: VapidKeyRepository, alert: Alert, sub: PushSubscription,
) -> RelayOutcome:
    try:
        vapid = await vapid_repo.get_or_create()
    except VapidUnavailableError as exc:
        raise PushUnavailableError(str(exc)) from exc

    payload = json.dumps(
        {
            "title": alert.title or "MailVerdict", "body": alert.body, "url": alert.url,
            # The same id the SSE-driven in-app Notification() call tags
            # itself with (see use-sse.ts) -- a page open and subscribed
            # at once gets one OS notification, not two, since same-tag
            # notifications from the same origin replace one another
            # whether raised from the page or from this push's own
            # service worker.
            "tag": str(alert.id),
        }
    )
    try:
        await webpush_async(
            subscription_info={
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
            },
            data=payload,
            vapid_private_key=vapid,
            vapid_claims={"sub": _VAPID_SUBJECT},
            ttl=_TTL_SECONDS,
        )
    except WebPushException as exc:
        return RelayOutcome.GONE if exc.status_code in (404, 410) else RelayOutcome.FAILED
    return RelayOutcome.DELIVERED


async def _send_native(
    db: DatabaseConnection,
    relay: RelayClient | None,
    alert: Alert,
    sub: PushSubscription,
    *,
    resolved: list[uuid.UUID],
) -> RelayOutcome:
    if relay is None or not relay.allows(sub.relay_url):
        raise PushUnavailableError("native push is unavailable for this subscription")
    assert sub.installation_id is not None
    try:
        ticket, key = relay.open_credentials(sub)
    except EncryptionError:
        # Stored under an ENCRYPTION_KEY this server no longer has -- the
        # row can never be sent to again; the app re-registers.
        logger.warning(
            "Native subscription unreadable under the current ENCRYPTION_KEY, removing it",
            extra={"subscription_id": str(sub.id)},
        )
        return RelayOutcome.GONE

    badge = await badge_count(db, get_settings_service(), subscription=sub)
    blob = seal_payload(
        alert_payload(alert, badge=badge, resolved=resolved), key, sub.installation_id,
    )
    return await relay.send_alert(
        sub, ticket=ticket, blob=blob, collapse_id=str(alert.id), ttl_seconds=_TTL_SECONDS,
    )
