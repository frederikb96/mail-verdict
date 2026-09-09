"""
The Trash retention sweep: an account with account_prefs.trash_retention_days
set gets its overdue trash permanently removed on a schedule -- the one
thing the pipeline's arrival-only trigger (pipeline/enqueue.py) cannot
express.

Retention means time spent sitting in Trash, not the message's own date
-- what every mail client's own "empty Trash after N days" already means,
and the only reading that cannot itself cause data loss (a message dated
years ago, dragged into Trash a moment ago, still gets the full grace
window). Nothing in the mirror records when a message moved into Trash,
so trash_entries (database/models.py) is this sweep's own clock: the
first tick that sees a message sitting in an account's Trash without a
row there stamps one, at that moment -- covering mail already sitting in
Trash before retention was ever turned on, and mail trashed from any
client other than this one, with the same mechanism. A message no longer
observed in Trash (moved out, or expunged by some other path) has its
row deleted, so returning to Trash later starts a fresh clock rather than
resuming the old one.

Every step here is written to fail toward waiting longer, never toward
deleting sooner: an orphaned trash_entries row (the message's id changed
under a UIDVALIDITY resync while it sat in Trash) is indistinguishable
from a message never seen before, so it is re-stamped rather than
expunged early. Only the third step is actually destructive and only it
is batch-limited the way outbox/pending.py's own periodic worker
documents -- this runs against a live mailbox, where a runaway sweep is
unrecoverable.
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
# _EXPUNGE_BATCH_SIZE, since the next tick simply continues where this one
# left off.
_SWEEP_INTERVAL_SECONDS = 900.0

# Bookkeeping only -- stamping a new entry or dropping a stale one never
# destroys a message, so these can run generously wide per tick without
# the same caution the actual expunge below needs.
_BOOKKEEPING_BATCH_SIZE = 2000
_EXPUNGE_BATCH_SIZE = 200


async def _sweep_trash_once(db: DatabaseConnection) -> None:
    """One tick, three steps, one transaction: stamp any message newly
    seen in a retention-configured account's Trash, drop the stamp for
    any message no longer there, then expunge whatever is now overdue."""
    async with db.session() as session:
        newly_stamped = await session.execute(
            text(
                """
                INSERT INTO trash_entries (message_id, account_id, entered_trash_at)
                SELECT m.id, m.account_id, now()
                FROM account_prefs ap
                JOIN messages m ON m.account_id = ap.account_id
                JOIN folders f ON f.id = m.folder_id
                LEFT JOIN folder_prefs fp ON fp.folder_id = f.id
                LEFT JOIN trash_entries te ON te.message_id = m.id
                WHERE ap.trash_retention_days IS NOT NULL
                  AND coalesce(fp.special_use_override, f.special_use, '') = 'trash'
                  AND m.expunged_at IS NULL
                  AND te.message_id IS NULL
                LIMIT :batch
                ON CONFLICT (message_id) DO NOTHING
                RETURNING message_id
                """
            ),
            {"batch": _BOOKKEEPING_BATCH_SIZE},
        )
        stamped_count = len(newly_stamped.all())

        # A message this table still names is no longer removed the
        # moment it stops sitting in that account's Trash -- moved
        # elsewhere, or expunged by some other path -- and dropping its
        # row here is what gives it a fresh clock if it is ever trashed
        # again, rather than resuming wherever the old one left off.
        # Not gated on the account's retention setting still being on --
        # whether a message is physically sitting in Trash does not
        # depend on it, and an account_prefs row is not needed to answer
        # that question at all. A row left behind by an account whose
        # retention was later turned off is still cleaned up the moment
        # its message actually leaves Trash, so re-enabling retention
        # never inherits a stale, no-longer-true entry.
        dropped = await session.execute(
            text(
                """
                DELETE FROM trash_entries te
                WHERE te.message_id IN (
                    SELECT te2.message_id
                    FROM trash_entries te2
                    LEFT JOIN messages m ON m.id = te2.message_id AND m.expunged_at IS NULL
                    LEFT JOIN folders f ON f.id = m.folder_id
                    LEFT JOIN folder_prefs fp ON fp.folder_id = f.id
                    WHERE m.id IS NULL
                       OR coalesce(fp.special_use_override, f.special_use, '') != 'trash'
                    LIMIT :batch
                )
                RETURNING te.message_id
                """
            ),
            {"batch": _BOOKKEEPING_BATCH_SIZE},
        )
        dropped_count = len(dropped.all())

        overdue = (
            await session.execute(
                text(
                    """
                    SELECT te.message_id, te.account_id
                    FROM trash_entries te
                    JOIN account_prefs ap ON ap.account_id = te.account_id
                    WHERE ap.trash_retention_days IS NOT NULL
                      AND te.entered_trash_at
                          < now() - make_interval(days => ap.trash_retention_days)
                    ORDER BY te.entered_trash_at
                    LIMIT :batch
                    """
                ),
                {"batch": _EXPUNGE_BATCH_SIZE},
            )
        ).all()

        removed = 0
        if overdue:
            message_ids = [row.message_id for row in overdue]
            removed = await expunge_bulk(session, message_ids)
            await session.execute(
                text("DELETE FROM trash_entries WHERE message_id = ANY(:ids)"),
                {"ids": message_ids},
            )

    if stamped_count or dropped_count or removed:
        logger.info(
            "Trash retention sweep",
            extra={"stamped": stamped_count, "dropped": dropped_count, "removed": removed},
        )


def build_trash_retention_timer(db: DatabaseConnection) -> ReconciliationTimer:
    """The advisory-locked periodic pass that permanently removes overdue
    trash -- one per process, safe with more than one replica."""

    async def _callback() -> None:
        await _sweep_trash_once(db)

    return ReconciliationTimer(db, _SWEEP_LOCK_KEY, _callback, _SWEEP_INTERVAL_SECONDS)
