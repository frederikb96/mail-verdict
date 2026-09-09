"""
The Trash retention sweep: an account with account_prefs.trash_retention_days
set gets every message in its trash folder older than that many days
permanently removed, on a schedule -- the one thing the pipeline's
arrival-only trigger (pipeline/enqueue.py) cannot express.

"Older" is the message's own date (received_at, falling back to
created_at for the rare row with neither), not how long it has sat in
Trash -- a message dragged into Trash the moment it arrives is removed on
the very next sweep if it is old mail, exactly as if it had been sitting
there for the retention window already. Batched and bounded per tick,
the same reasoning outbox/pending.py's own periodic worker documents:
this runs against a live mailbox, where a runaway sweep -- unlike a
notification a few seconds late -- is unrecoverable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import text

from mail_verdict.postimap.actions import expunge_bulk
from mail_verdict.queue.notify import ReconciliationTimer

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

# Distinct from every other ReconciliationTimer's lock key in the process
# (pipeline/enqueue.py's 761_034_331, outbox/pending.py's 761_034_500,
# alerts/dispatch.py's 761_034_600).
_SWEEP_LOCK_KEY = 761_034_700

# A cleanup task, not a latency-sensitive one -- checking every 15 minutes
# costs nothing an account with retention off ever notices, and keeps a
# very large overdue backlog from being swept in one pass regardless of
# _SWEEP_BATCH_SIZE, since the next tick simply continues where this one
# left off.
_SWEEP_INTERVAL_SECONDS = 900.0
_SWEEP_BATCH_SIZE = 200


async def _sweep_trash_once(db: DatabaseConnection) -> None:
    """One tick: expunge up to _SWEEP_BATCH_SIZE overdue trash messages,
    across every account with retention configured, oldest first."""
    async with db.session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT m.id, m.account_id
                    FROM account_prefs ap
                    JOIN messages m ON m.account_id = ap.account_id
                    JOIN folders f ON f.id = m.folder_id
                    LEFT JOIN folder_prefs fp ON fp.folder_id = f.id
                    WHERE ap.trash_retention_days IS NOT NULL
                      AND coalesce(fp.special_use_override, f.special_use, '') = 'trash'
                      AND m.expunged_at IS NULL
                      AND coalesce(m.received_at, m.created_at)
                          < now() - make_interval(days => ap.trash_retention_days)
                    ORDER BY coalesce(m.received_at, m.created_at)
                    LIMIT :batch
                    """
                ),
                {"batch": _SWEEP_BATCH_SIZE},
            )
        ).all()
        if not rows:
            return

        message_ids = [row.id for row in rows]
        affected = await expunge_bulk(session, message_ids)

        accounts_touched = {row.account_id for row in rows}
        logger.info(
            "Trash retention sweep",
            extra={"removed": affected, "accounts": len(accounts_touched)},
        )


def build_trash_retention_timer(db: DatabaseConnection) -> ReconciliationTimer:
    """The advisory-locked periodic pass that permanently removes overdue
    trash -- one per process, safe with more than one replica."""

    async def _callback() -> None:
        await _sweep_trash_once(db)

    return ReconciliationTimer(db, _SWEEP_LOCK_KEY, _callback, _SWEEP_INTERVAL_SECONDS)
