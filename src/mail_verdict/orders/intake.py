"""
The thread follow-up hook: a reply Freddy sends inside an order's
conversation, and the shop's own answer to it, join that order even when
neither mail's subject says anything about orders, tickets or bookings.

Called from server.py's `message`/`insert` branch, right after
enqueue_live_arrival -- the live-arrival path already covers a shop's
reply landing in the inbox (the pipeline's own `orders` stage sees it and
its own thread bypass rule applies); this hook covers the other half,
the outgoing side no pipeline stage ever runs on: a message sent from
this account, landing in Sent.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from mail_verdict.database.models import AccountPrefs, Folder, FolderPrefs, Message, OrderMail
from mail_verdict.orders.repository import enqueue_mail_job
from mail_verdict.queue.notify import WorkQueueNotifier

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.postimap.listener import PostimapEvent


async def enqueue_thread_follow_up(db: DatabaseConnection, event: PostimapEvent) -> None:
    """A no-op unless: the account has orders switched on, this message
    landed in a folder whose effective special use is "sent", and its
    thread already belongs to an order."""
    try:
        account_id = uuid.UUID(event.account_id)
        message_id = uuid.UUID(event.id)
        folder_id = uuid.UUID(event.folder_id) if event.folder_id else None
    except ValueError:
        return
    if folder_id is None:
        return

    async with db.session() as session:
        prefs_result = await session.execute(
            select(AccountPrefs.orders_enabled).where(AccountPrefs.account_id == account_id)
        )
        if not (prefs_result.scalar_one_or_none() or False):
            return

        folder_result = await session.execute(
            select(func.coalesce(FolderPrefs.special_use_override, Folder.special_use))
            .select_from(Folder)
            .outerjoin(FolderPrefs, Folder.id == FolderPrefs.folder_id)
            .where(Folder.id == folder_id)
        )
        if folder_result.scalar_one_or_none() != "sent":
            return

        msg_result = await session.execute(
            select(Message.thread_id, Message.message_id, Message.received_at).where(
                Message.id == message_id, Message.account_id == account_id,
            )
        )
        msg_row = msg_result.one_or_none()
        if msg_row is None or msg_row.thread_id is None:
            return

        thread_result = await session.execute(
            select(OrderMail.id)
            .where(OrderMail.account_id == account_id, OrderMail.thread_id == msg_row.thread_id)
            .limit(1)
        )
        if thread_result.scalar_one_or_none() is None:
            return

        msg_key = msg_row.message_id or str(message_id)
        inserted = await enqueue_mail_job(
            session, account_id=account_id, msg_key=msg_key, message_id=message_id,
            origin="thread", priority=0, filter_reason="thread",
            next_attempt_at=msg_row.received_at or datetime.now(timezone.utc),
        )
        if inserted:
            await WorkQueueNotifier.notify(session, "orders")
