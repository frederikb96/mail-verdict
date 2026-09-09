"""
Turning a live mail arrival into an alert -- called directly from the
`message`/`insert` branch of server.py's postimap event dispatcher,
gated the same way enqueue_live_arrival is: origin == "sync" only, never
a backfill, so historical mail synced for the first time is never turned
into an alert any more than it is ever classified.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from mail_verdict.database.models import Alert, Message
from mail_verdict.database.msg_key import compute_msg_key
from mail_verdict.database.repository import AlertRepository
from mail_verdict.push.send import dispatch_push_for_alert

if TYPE_CHECKING:
    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.push.vapid import VapidKeyRepository

logger = logging.getLogger(__name__)

# A sender line long enough to be useful, short enough that a bell
# dropdown row stays one line -- the same rough shape a notification
# popup itself would truncate to.
_BODY_MAX_LEN = 120

# asyncio holds only a weak reference to a task nothing else is holding
# a reference to, so a fire-and-forget push dispatch can be garbage
# collected mid-flight the moment this function returns. Kept here for
# exactly as long as the task runs, and discarded via its own done
# callback once it finishes.
_background_push_tasks: set[asyncio.Task[None]] = set()


async def create_mail_alert_for_arrival(
    db: DatabaseConnection,
    event_ring: EventRing | None,
    vapid_repo: VapidKeyRepository | None = None,
    *,
    account_id: uuid.UUID,
    message_id: uuid.UUID,
    folder_id: uuid.UUID | None = None,
) -> None:
    """
    Insert an alert for a newly-arrived message and, if this is genuinely
    a new alert (not a duplicate the unique dedupe_key absorbed), push
    alert.new so an open page can raise a notification and refresh its
    unseen count immediately -- the SSE round trip is what makes the
    in-app path need no polling -- and, if a VapidKeyRepository was
    passed, hand the alert to a background task that pushes it to every
    subscription that wants it (push/send.py).

    folder_id rides along on the live event, the push dispatch, and the
    row itself. "Which folders alert" is a client-side preference for a
    browser with no push subscription (see the SSE handler and
    alert-prefs.ts) and a server-side one (push_subscriptions.
    alert_folder_ids) for a subscribed device -- both read this one
    threaded-through value rather than re-querying the message's folder,
    and the durable list (AlertRepository.list_recent/unseen_count) reads
    the same value back off the row it was stored on, so the three
    surfaces agree on what "which folders alert" means. The caller
    already has it (the postimap event that triggered this).

    The push dispatch runs as a fire-and-forget background task rather
    than being awaited here: it makes outbound HTTPS requests to however
    many push services the reader has devices registered with, and this
    function runs inline in the postimap event listener -- awaiting it
    would delay every event still queued behind this one.

    A message already gone by the time this runs (expunged between the
    insert and this call) is skipped rather than raising -- an alert for
    mail nobody can open any more is worse than no alert.
    """
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
        return

    msg_key = compute_msg_key(
        account_id=account_id, message_id_hdr=row.message_id, from_addr=row.from_addr,
        subject=row.subject, received_at=row.received_at, size_bytes=row.size_bytes,
    )
    body = row.from_addr or None
    if body and len(body) > _BODY_MAX_LEN:
        body = body[:_BODY_MAX_LEN] + "…"

    alert = await AlertRepository(db).create_mail_alert(
        account_id=account_id, message_id=message_id, msg_key=msg_key,
        title=row.subject or "(no subject)", body=body, folder_id=folder_id,
    )
    if alert is None:
        return

    if vapid_repo is not None:
        # Fire-and-forget: _dispatch_push_safe below never lets an
        # exception escape, so there is nothing for a caller to await.
        push_task = asyncio.create_task(
            _dispatch_push_safe(db, vapid_repo, alert, folder_id=folder_id)
        )
        _background_push_tasks.add(push_task)
        push_task.add_done_callback(_background_push_tasks.discard)

    if event_ring is None:
        return

    await event_ring.add(
        account_id, "alert.new",
        {
            "id": str(alert.id), "kind": alert.kind, "title": alert.title,
            "body": alert.body, "url": alert.url, "account_id": str(account_id),
            "folder_id": str(folder_id) if folder_id else None,
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
