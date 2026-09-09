"""
Turning a live mail arrival into an alert -- called directly from the
`message`/`insert` branch of server.py's postimap event dispatcher,
gated the same way enqueue_live_arrival is: origin == "sync" only, never
a backfill, so historical mail synced for the first time is never turned
into an alert any more than it is ever classified.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from mail_verdict.database.models import Message
from mail_verdict.database.msg_key import compute_msg_key
from mail_verdict.database.repository import AlertRepository

if TYPE_CHECKING:
    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

# A sender line long enough to be useful, short enough that a bell
# dropdown row stays one line -- the same rough shape a notification
# popup itself would truncate to.
_BODY_MAX_LEN = 120


async def create_mail_alert_for_arrival(
    db: DatabaseConnection,
    event_ring: EventRing | None,
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
    in-app path need no polling.

    folder_id rides along on the live event only -- alerts carries no
    folder_id column of its own (only account_id/message_id, the same
    source-coordinate shape every alert kind uses), since "which folders
    alert" is a per-browser preference read client-side from this one
    live field, not a server-side filter the durable row needs to carry.
    The caller already has it (the postimap event that triggered this),
    so it is threaded through rather than re-queried.

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
        title=row.subject or "(no subject)", body=body,
    )
    if alert is None or event_ring is None:
        return

    await event_ring.add(
        account_id, "alert.new",
        {
            "id": str(alert.id), "kind": alert.kind, "title": alert.title,
            "body": alert.body, "url": alert.url, "account_id": str(account_id),
            "folder_id": str(folder_id) if folder_id else None,
        },
    )
