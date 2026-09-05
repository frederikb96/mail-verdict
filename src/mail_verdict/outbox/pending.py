"""
The undo-send staging table: hold a composed send for its grace window,
move it into outbox once the window passes uncancelled.

Inserting into PostIMAP's outbox table is itself the instruction to send
-- there is no consumer-writable column expressing a delay or a hold (see
outbox_attachments' own docstring in database/models.py). A send with a
nonzero undo window is therefore composed and written here first; the
periodic worker below claims a due, uncancelled row and performs the real
insert_outbox() write in the same transaction that removes it from this
table, so a row is never simultaneously live in outbox and still
cancellable. The worker passes this row's own id into insert_outbox() as
the outbox row's id, so a caller who accepted the send at staging time can
keep resolving it under that same id once it lands in outbox -- see
api/outbox.py's list_outbox(), which lists a still-staged row alongside
real outbox rows for exactly this reason.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import PendingSend, PendingSendAttachment
from mail_verdict.postimap.actions import insert_outbox
from mail_verdict.queue.notify import ReconciliationTimer

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

_PENDING_SEND_LOCK_KEY = 761_034_500

# A row's own grace window is measured in single-digit seconds, so the
# worker checking for one that has passed needs a poll far tighter than
# the pipeline reconciler's 30-second interval -- this is an
# implementation constant, not something a person would tune, unlike
# settings.outbox.undo_send_seconds itself.
_POLL_INTERVAL_SECONDS = 1.0

_CLAIM_BATCH_SIZE = 20

PendingAttachment = tuple[str, str | None, bytes, str | None]


async def stage_send(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    from_addr: str | None,
    to_addrs: list[str] | None,
    cc_addrs: list[str] | None,
    bcc_addrs: list[str] | None,
    subject: str | None,
    body_text: str | None,
    body_html: str | None,
    in_reply_to: str | None,
    references: list[str] | None,
    replaces_message_id: uuid.UUID | None,
    attachments: list[PendingAttachment],
    undo_seconds: float,
) -> PendingSend:
    """
    Insert a PendingSend row (and its attachments), due `undo_seconds` from now.

    Args:
        session: Active AsyncSession (caller commits)
        account_id: Account to send from
        from_addr: Sender address, already resolved -- the same value
            insert_outbox() would eventually be called with
        to_addrs: Recipient addresses
        cc_addrs: CC addresses
        bcc_addrs: BCC addresses
        subject: Message subject
        body_text: Plain text body
        body_html: HTML body, optional
        in_reply_to: The replied-to message's Message-ID header value
        references: Full References chain for threading
        replaces_message_id: The messages.id of the draft this row replaces
        attachments: (filename, content_type, data, content_id) tuples
        undo_seconds: Grace window before this becomes a real send

    Returns:
        The inserted PendingSend row (flushed, not yet committed)
    """
    row = PendingSend(
        account_id=account_id,
        from_addr=from_addr,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        bcc_addrs=bcc_addrs,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        in_reply_to=in_reply_to,
        msg_references=references,
        replaces_message_id=replaces_message_id,
        send_after=datetime.now(timezone.utc) + timedelta(seconds=undo_seconds),
    )
    session.add(row)
    await session.flush()

    for filename, content_type, data, content_id in attachments:
        session.add(
            PendingSendAttachment(
                pending_send_id=row.id,
                filename=filename,
                content_type=content_type,
                data=data,
                content_id=content_id,
            )
        )

    await session.flush()
    await session.refresh(row)
    return row


async def cancel_pending_send(session: AsyncSession, pending_send_id: uuid.UUID) -> bool:
    """
    Cancel a pending send, if it has not already been claimed by the worker.

    A single UPDATE is the whole race resolution: the worker's own claim
    query below locks a due row with SELECT ... FOR UPDATE inside the same
    transaction it deletes the row in, so this UPDATE either lands first
    (the worker's claim then excludes the row, since cancelled_at is no
    longer NULL) or blocks until the worker's transaction commits and the
    row is simply gone -- either way the two can never both believe they
    won.

    Args:
        session: Active AsyncSession (caller commits)
        pending_send_id: The PendingSend row to cancel

    Returns:
        True if this call cancelled it; False if it no longer exists or
        was already cancelled -- either means it is too late
    """
    result = await session.execute(
        update(PendingSend)
        .where(PendingSend.id == pending_send_id, PendingSend.cancelled_at.is_(None))
        .values(cancelled_at=func.now())
    )
    return (result.rowcount or 0) > 0  # type: ignore[attr-defined]


async def list_pending_sends(
    session: AsyncSession, account_id: uuid.UUID | None,
) -> list[PendingSend]:
    """Uncancelled pending sends, soonest first -- for the undo banner to
    rehydrate from on a fresh page load."""
    stmt = (
        select(PendingSend)
        .where(PendingSend.cancelled_at.is_(None))
        .order_by(PendingSend.send_after)
    )
    if account_id is not None:
        stmt = stmt.where(PendingSend.account_id == account_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _process_due_sends(db: DatabaseConnection) -> None:
    """One tick: claim every due, uncancelled row and move it into outbox."""
    async with db.session() as session:
        result = await session.execute(
            select(PendingSend)
            .where(
                PendingSend.cancelled_at.is_(None),
                PendingSend.send_after <= func.now(),
            )
            .order_by(PendingSend.send_after)
            .limit(_CLAIM_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        due = list(result.scalars().all())
        if not due:
            return

        for row in due:
            att_result = await session.execute(
                select(PendingSendAttachment).where(
                    PendingSendAttachment.pending_send_id == row.id,
                )
            )
            attachments: list[PendingAttachment] = [
                (a.filename or "attachment", a.content_type, a.data, a.content_id)
                for a in att_result.scalars().all()
            ]
            try:
                await insert_outbox(
                    session,
                    id=row.id,
                    account_id=row.account_id,
                    kind="send",
                    from_addr=row.from_addr,
                    to_addrs=row.to_addrs,
                    cc_addrs=row.cc_addrs,
                    bcc_addrs=row.bcc_addrs,
                    subject=row.subject,
                    body_text=row.body_text,
                    body_html=row.body_html,
                    in_reply_to=row.in_reply_to,
                    references=row.msg_references,
                    replaces_message_id=row.replaces_message_id,
                    attachments=attachments,
                )
            except Exception:
                # Most plausibly a replaces_message_id whose draft was
                # deleted independently during the grace window (a real FK
                # violation at insert time -- see insert_outbox's own
                # docstring). Left in place rather than deleted: the row
                # is no longer due (send_after is in the past but the next
                # tick reattempts it identically and fails identically),
                # so it sits here for manual inspection rather than being
                # silently dropped or retried forever.
                logger.exception(
                    "Failed to move a due pending send into outbox",
                    extra={"pending_send_id": str(row.id)},
                )
                continue
            await session.delete(row)


def build_pending_send_timer(db: DatabaseConnection) -> ReconciliationTimer:
    """The periodic pass that turns a due, uncancelled pending send into a
    real outbox row -- advisory-locked so only one replica runs it."""

    async def _callback() -> None:
        await _process_due_sends(db)

    return ReconciliationTimer(db, _PENDING_SEND_LOCK_KEY, _callback, _POLL_INTERVAL_SECONDS)
