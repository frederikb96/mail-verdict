"""
The retention sweep: an account with a retention period configured for
Trash or Junk gets whatever has sat in that folder longer than its own
period permanently removed, on a schedule -- the one thing the
pipeline's arrival-only trigger (pipeline/enqueue.py) cannot express.

Retention means time spent sitting in the folder, not the message's own
date -- what every mail client's own "empty Trash/Junk after N days"
already means, and the only reading that cannot itself cause data loss
(a message dated years ago, dragged in a moment ago, still gets the full
grace window). Nothing in the mirror records when a message moved into
either folder, so retention_entries (database/models.py) is this sweep's
own clock: the first tick that sees a message sitting in an account's
Trash or Junk without a row there stamps one, at that moment -- covering
mail already sitting there before retention was ever turned on, mail
trashed or spam-filed from any client, and Junk mail the spam pipeline
filed on its own with nobody having touched it, all with the same
mechanism. A message no longer observed in that role's folder (moved
out, expunged, or reassigned to the other role directly) has its row
dropped or overwritten, so returning later starts a fresh clock rather
than resuming the old one.

Trash and Junk share every mechanic here, aged against each account's
own trash_retention_days / junk_retention_days -- independently
configurable, deliberately not one setting applied to both. One sweep,
parameterised by role, rather than a second parallel implementation: two
copies of the same three steps would agree the day they were written and
drift apart the first time only one of them was fixed.

Every step here is written to fail toward waiting longer, never toward
deleting sooner: an orphaned row (the message's id changed under a
UIDVALIDITY resync while it sat in a tracked folder) is indistinguishable
from a message never seen before, so it is re-stamped rather than
expunged early. Only the actual removal is batch-limited; bookkeeping is
cheap enough to run unbounded per tick.
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

# role -> the account_prefs column holding that role's own retention
# period. The only place the two roles' names are spelled out; every
# query below is parameterised on this, never duplicated per role.
_RETENTION_ROLES: dict[str, str] = {
    "trash": "trash_retention_days",
    "junk": "junk_retention_days",
}


async def _sweep_retention_once(db: DatabaseConnection) -> None:
    """One tick, once per role, each in its own transaction -- see
    _sweep_role_once for the three steps every role goes through."""
    for role, retention_column in _RETENTION_ROLES.items():
        await _sweep_role_once(db, role=role, retention_column=retention_column)


async def _sweep_role_once(db: DatabaseConnection, *, role: str, retention_column: str) -> None:
    """
    One role's tick, three steps, one transaction: stamp any message
    newly seen in a retention-configured account's folder for this role,
    reconcile any row that no longer belongs to this role (moved out, or
    moved directly into the *other* role's folder without ever leaving
    a tracked one), then expunge whatever is now overdue.

    retention_column is one of the two hardcoded values in
    _RETENTION_ROLES, never anything caller-supplied, so interpolating it
    into the query text below carries no injection risk -- there is no
    other way to parameterise a column name as a bind parameter.
    """
    async with db.session() as session:
        newly_stamped = await session.execute(
            text(
                f"""
                INSERT INTO retention_entries (message_id, account_id, role, entered_at)
                SELECT m.id, m.account_id, :role, now()
                FROM account_prefs ap
                JOIN messages m ON m.account_id = ap.account_id
                JOIN folders f ON f.id = m.folder_id
                LEFT JOIN folder_prefs fp ON fp.folder_id = f.id
                LEFT JOIN retention_entries re ON re.message_id = m.id
                WHERE ap.{retention_column} IS NOT NULL
                  AND coalesce(fp.special_use_override, f.special_use, '') = :role
                  AND m.expunged_at IS NULL
                  AND re.message_id IS NULL
                LIMIT :batch
                ON CONFLICT (message_id) DO NOTHING
                RETURNING message_id
                """
            ),
            {"role": role, "batch": _BOOKKEEPING_BATCH_SIZE},
        )
        stamped_count = len(newly_stamped.all())

        # ON CONFLICT DO NOTHING rather than an upsert: a message moved
        # directly from one tracked role's folder into the other's,
        # without ever passing through an untracked folder in between,
        # still has its old role's row here for one more tick -- this
        # role's own stamp step above sees an existing message_id and
        # skips it, same as any other conflict. The OTHER role's cleanup
        # step (the one whose folder this message just left) drops that
        # stale row on this same tick, and the next tick's stamp then
        # claims it cleanly under its new role. One tick of delay in the
        # rarer direction, never an early deletion or a lost clock --
        # the same fail-toward-later bias as everything else here.
        #
        # A message this role's own entries still name is no longer
        # tracked the moment it stops sitting in that role's folder --
        # moved elsewhere, or expunged by some other path -- and dropping
        # its row here is what gives it a fresh clock if it is ever filed
        # into this role again, rather than resuming wherever the old one
        # left off.
        dropped = await session.execute(
            text(
                """
                DELETE FROM retention_entries re
                WHERE re.role = :role
                  AND re.message_id IN (
                    SELECT re2.message_id
                    FROM retention_entries re2
                    LEFT JOIN messages m ON m.id = re2.message_id AND m.expunged_at IS NULL
                    LEFT JOIN folders f ON f.id = m.folder_id
                    LEFT JOIN folder_prefs fp ON fp.folder_id = f.id
                    WHERE re2.role = :role
                      AND (
                        m.id IS NULL
                        OR coalesce(fp.special_use_override, f.special_use, '') != :role
                      )
                    LIMIT :batch
                )
                RETURNING re.message_id
                """
            ),
            {"role": role, "batch": _BOOKKEEPING_BATCH_SIZE},
        )
        dropped_count = len(dropped.all())

        overdue = (
            await session.execute(
                text(
                    f"""
                    SELECT re.message_id, re.account_id
                    FROM retention_entries re
                    JOIN account_prefs ap ON ap.account_id = re.account_id
                    WHERE re.role = :role
                      AND ap.{retention_column} IS NOT NULL
                      AND re.entered_at
                          < now() - make_interval(days => ap.{retention_column})
                    ORDER BY re.entered_at
                    LIMIT :batch
                    """
                ),
                {"role": role, "batch": _EXPUNGE_BATCH_SIZE},
            )
        ).all()

        removed = 0
        if overdue:
            message_ids = [row.message_id for row in overdue]
            removed = await expunge_bulk(session, message_ids)
            await session.execute(
                text("DELETE FROM retention_entries WHERE message_id = ANY(:ids)"),
                {"ids": message_ids},
            )

    if stamped_count or dropped_count or removed:
        logger.info(
            "Retention sweep",
            extra={
                "role": role, "stamped": stamped_count, "dropped": dropped_count,
                "removed": removed,
            },
        )


def build_retention_timer(db: DatabaseConnection) -> ReconciliationTimer:
    """The advisory-locked periodic pass that permanently removes overdue
    Trash and Junk -- one per process, safe with more than one replica."""

    async def _callback() -> None:
        await _sweep_retention_once(db)

    return ReconciliationTimer(db, _SWEEP_LOCK_KEY, _callback, _SWEEP_INTERVAL_SECONDS)
