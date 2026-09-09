"""
Sending an alert to every push subscription that wants it.

Generic over alert kind on purpose -- a mail alert (delivered immediately,
folder-scoped) and a future reminder alert (delivered by a scheduler,
gated on reminders_enabled) both end here, so there is one place that
knows about subscriptions and one cleanup rule: a 404 or 410 from the push
service is its own protocol-level unsubscribe signal (RFC 8030 s7), not a
transient failure to retry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING

from pywebpush import WebPushException, webpush_async

from mail_verdict.database.repository import PushSubscriptionRepository
from mail_verdict.push.vapid import VapidUnavailableError

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.database.models import Alert, PushSubscription
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


async def dispatch_push_for_alert(
    db: DatabaseConnection,
    vapid_repo: VapidKeyRepository,
    alert: Alert,
    *,
    folder_id: uuid.UUID | None,
) -> None:
    """
    Push `alert` to every subscription eligible for it.

    Never raises: an unconfigured ENCRYPTION_KEY or a subscriber's own
    outage must never affect the alert row or the in-app SSE path, both
    already delivered by the caller before this is reached.

    Args:
        db: Database connection
        vapid_repo: This server's VAPID identity
        alert: The alert to send -- already inserted and, for a "mail"
            alert, already delivered_at-stamped
        folder_id: The message's folder, threaded through rather than
            re-queried, for a "mail" alert's per-subscription folder
            filter (alerts itself carries no folder_id column of its own
            -- see alerts/dispatch.py). Ignored for "reminder".
    """
    try:
        vapid = await vapid_repo.get_or_create()
    except VapidUnavailableError:
        return

    repo = PushSubscriptionRepository(db)
    subscriptions = await repo.list_for_alert(kind=alert.kind, folder_id=folder_id)
    if not subscriptions:
        return

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
    results = await asyncio.gather(
        *(_send_one(repo, vapid, sub, payload) for sub in subscriptions),
        return_exceptions=True,
    )
    for sub, result in zip(subscriptions, results):
        if isinstance(result, Exception):
            logger.warning("Unexpected error sending push to a subscription: %s", result)


async def _send_one(
    repo: PushSubscriptionRepository,
    vapid: object,
    sub: PushSubscription,
    payload: str,
) -> None:
    """One subscription's send, with its own outcome handled here rather
    than left to the caller -- a 404/410 (this endpoint will never accept
    another push) drops the row, anything else is stamped failed_at and
    left for the next alert to try again."""
    subscription_info = {
        "endpoint": sub.endpoint,
        "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
    }
    try:
        await webpush_async(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=vapid,
            vapid_claims={"sub": _VAPID_SUBJECT},
            ttl=_TTL_SECONDS,
        )
    except WebPushException as exc:
        if exc.status_code in (404, 410):
            await repo.delete(sub.id)
        else:
            await repo.mark_failed(sub.id)
        return
    await repo.mark_seen(sub.id)
