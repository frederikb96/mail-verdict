"""
The server's own guarantee that one message is sent once, whatever the
client does.

Two independent checks, because they catch different repeats:

- An idempotency key the caller generates per composed message. The same
  request arriving twice -- a double press, a retry after a lost response
  -- finds the first one's OutboxSubmission row and is answered with that
  row rather than creating another.
- A draft already on its way out. Two sends naming the same
  replaces_message_id are the same message sent twice even when they share
  nothing else, including a key -- a client that lost its own guard cannot
  be relied on to send one. Only a send that can no longer reach anyone
  frees the draft again: one undone inside its window, or one PostIMAP
  dead-lettered (the draft is removed only once a send succeeds, so it is
  still there to resend).

Each check runs under a transaction-scoped advisory lock on the thing it
is about, so two concurrent requests are serialised rather than both
reading "nothing yet" -- the second waits for the first to commit and then
sees its row. The lock is released at commit or rollback.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import Outbox, OutboxSubmission, PendingSend


async def _lock(session: AsyncSession, name: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))"), {"name": name},
    )


async def find_submission(
    session: AsyncSession, idempotency_key: uuid.UUID,
) -> OutboxSubmission | None:
    """
    The earlier submission carrying this key, if any -- holding the key's
    lock until the caller's transaction ends, so a concurrent request with
    the same key cannot also find none.

    Args:
        session: Active AsyncSession (caller commits)
        idempotency_key: The caller's key for this composed message

    Returns:
        The earlier OutboxSubmission, or None if this is the first
    """
    await _lock(session, f"outbox-submission:{idempotency_key}")
    previous: OutboxSubmission | None = await session.scalar(
        select(OutboxSubmission).where(OutboxSubmission.idempotency_key == idempotency_key)
    )
    return previous


async def record_submission(
    session: AsyncSession, idempotency_key: uuid.UUID, kind: str, target_id: uuid.UUID,
) -> None:
    """Record the row a keyed request created, in the same transaction as
    the row itself -- a request that fails before commit leaves the key
    unused, so retrying it after fixing whatever failed still works."""
    session.add(OutboxSubmission(idempotency_key=idempotency_key, kind=kind, target_id=target_id))
    await session.flush()


async def draft_send_in_flight(session: AsyncSession, draft_message_id: uuid.UUID) -> bool:
    """
    Whether a send naming this draft is already staged or queued -- holding
    the draft's lock until the caller's transaction ends.

    Args:
        session: Active AsyncSession (caller commits)
        draft_message_id: The messages.id of the draft being sent

    Returns:
        True if an uncancelled staged send, or a send outbox row that is
        not dead, already names it
    """
    await _lock(session, f"outbox-draft-send:{draft_message_id}")
    staged = await session.scalar(
        select(PendingSend.id)
        .where(
            PendingSend.replaces_message_id == draft_message_id,
            PendingSend.cancelled_at.is_(None),
        )
        .limit(1)
    )
    if staged is not None:
        return True
    queued = await session.scalar(
        select(Outbox.id)
        .where(
            Outbox.replaces_message_id == draft_message_id,
            Outbox.kind == "send",
            Outbox.status != "dead",
        )
        .limit(1)
    )
    return queued is not None
