"""
Mail in Archive or Trash is read, however it got there -- filed from this
application, by a rule, or by another mail client entirely. Trash always;
Archive unless settings.mail.mark_read_on_file_to_archive_or_junk is off,
the same setting that decides whether archiving here marks read.

Two halves. mark_read_on_landing reacts to the postimap event that lands a
message in either folder (filing/landing.py), which is what makes it
prompt. The reconcile timer is the net under it, for what no event covers:
a NOTIFY lost across a listener reconnect, a folder's first sync (which
suppresses per-message events), and mail already sitting there before this
existed. The same tick also sweeps for mail alerts whose mail is read
(alerts/resolve.py), for the same lost-event reason.

The reconcile has to stay cheap against an archive of a million messages,
and this application cannot add an index to PostIMAP's messages table
(DDL there needs ownership), so it is built on what already exists:

- folders.unread_count, maintained by PostIMAP's own trigger on every flag
  change, move and expunge, gates each folder. A folder with nothing
  unread costs one primary-key read and nothing more, which is every
  folder on every tick once caught up.
- Otherwise the folder is read one window of UIDs at a time, through
  whichever of PostIMAP's two (folder_id, imap_uid) indexes the planner
  picks (see WINDOW_SQL for why a window is a UID range). The top window
  comes first on every tick, since a message another client moves in gets
  the folder's next UID, along with the rows moved here that have no UID
  yet. Only while the count says unread mail remains below that does a
  cursor walk one more window per tick further down, starting over from
  the top once it reaches UID 1. No tick reads more than two windows of
  UIDs per folder, whatever the folder's size.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import text

from mail_verdict.alerts.resolve import announce_alerts_dismissed, resolve_all
from mail_verdict.database.repository import FolderRepository
from mail_verdict.filing.landing import landing_from_event, landing_role
from mail_verdict.postimap.actions import mark_seen_if_live
from mail_verdict.queue.notify import ReconciliationTimer

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.postimap.listener import PostimapEvent
    from mail_verdict.settings.service import SettingsService

logger = logging.getLogger(__name__)

# Distinct from every other ReconciliationTimer's lock key in the process
# (pipeline/enqueue.py's 761_034_331, outbox/pending.py's 761_034_500,
# alerts/dispatch.py's 761_034_600, retention/sweep.py's 761_034_700).
_RECONCILE_LOCK_KEY = 761_034_800

# A safety net under the event path, not the path itself -- a straggler
# waiting a minute is fine, and a caught-up tick costs a handful of
# primary-key reads.
_RECONCILE_INTERVAL_SECONDS = 60.0

# Rows examined per window, so per folder a tick reads at most twice this.
_WINDOW = 500

# folder id -> the imap_uid the deep walk continues below, per process. Lost
# on restart, which only means the walk starts over from the top.
Cursors = dict[uuid.UUID, int]


def always_read_roles(settings_service: SettingsService) -> tuple[str, ...]:
    """The folder roles whose mail is kept read, under the current settings."""
    if settings_service.get("mail")["mark_read_on_file_to_archive_or_junk"]:
        return ("archive", "trash")
    return ("trash",)


async def mark_read_on_landing(
    db: DatabaseConnection, settings_service: SettingsService, event: PostimapEvent,
) -> int:
    """
    Mark a message read if this event lands it in Archive or Trash.

    Args:
        db: Database connection
        settings_service: Current settings
        event: Any parsed postimap_events payload

    Returns:
        1 if the message was marked read, 0 otherwise (not a landing, not
        one of those folders, already read, or already gone)
    """
    landing = landing_from_event(event)
    if landing is None:
        return 0
    if await landing_role(db, landing, always_read_roles(settings_service)) is None:
        return 0
    async with db.session() as session:
        return await mark_seen_if_live(session, [landing.message_id])


# One window: a folder's live rows whose imap_uid lies in [:low_uid,
# :high_uid). A range of UIDs rather than a count of live rows, because
# PostIMAP has two (folder_id, imap_uid) indexes and the planner may pick
# either: its unique constraint's index still carries expunged rows, so
# "the next N live rows" through it would read every expunged row in
# between. A UID is unique within a folder, so a range reads at most its own
# width in index entries, live or expunged, through either index.
WINDOW_SQL = """
    SELECT id, imap_uid, is_seen FROM messages
    WHERE folder_id = :folder_id AND expunged_at IS NULL
      AND imap_uid >= :low_uid AND imap_uid < :high_uid
"""

# Rows moved here that have no UID yet -- none of the UID ranges above
# ever contains them.
PENDING_SQL = """
    SELECT id, imap_uid, is_seen FROM messages
    WHERE folder_id = :folder_id AND imap_uid IS NULL AND expunged_at IS NULL
    LIMIT :window
"""

# Where the top window ends: one index lookup on either index.
TOP_UID_SQL = "SELECT max(imap_uid) FROM messages WHERE folder_id = :folder_id"


async def _rows(
    session: AsyncSession, sql: str, params: dict[str, object],
) -> list[tuple[uuid.UUID, int | None, bool]]:
    """A window query's rows as (id, imap_uid, is_seen)."""
    result = await session.execute(text(sql), params)
    return [(row.id, row.imap_uid, row.is_seen) for row in result]


async def _reconcile_folder(db: DatabaseConnection, folder_id: uuid.UUID, cursors: Cursors) -> int:
    """Mark read whatever unread mail this tick's windows of one folder
    find. Returns how many rows it marked."""
    async with db.session() as session:
        unread = (
            await session.execute(
                text("SELECT unread_count FROM folders WHERE id = :id"), {"id": folder_id},
            )
        ).scalar_one_or_none()
        if not unread:
            cursors.pop(folder_id, None)
            return 0

        rows = await _rows(session, PENDING_SQL, {"folder_id": folder_id, "window": _WINDOW})
        top_uid = (
            await session.execute(text(TOP_UID_SQL), {"folder_id": folder_id})
        ).scalar_one_or_none()
        if top_uid is not None:
            high = top_uid + 1
            low = max(high - _WINDOW, 1)
            rows += await _rows(
                session, WINDOW_SQL, {"folder_id": folder_id, "low_uid": low, "high_uid": high},
            )
            found = sum(1 for _id, _uid, seen in rows if not seen)
            if low > 1 and unread > found:
                deep_high = min(cursors.get(folder_id, low), low)
                deep_low = max(deep_high - _WINDOW, 1)
                rows += await _rows(
                    session, WINDOW_SQL,
                    {"folder_id": folder_id, "low_uid": deep_low, "high_uid": deep_high},
                )
                if deep_low > 1:
                    cursors[folder_id] = deep_low
                else:
                    cursors.pop(folder_id, None)

        return await mark_seen_if_live(
            session, [message_id for message_id, _uid, seen in rows if not seen],
        )


async def reconcile_read_state_once(
    db: DatabaseConnection,
    event_ring: EventRing | None,
    settings_service: SettingsService,
    cursors: Cursors | None = None,
) -> tuple[int, int]:
    """
    One tick: every active account's Archive and Trash, then the mail
    alert sweep.

    Args:
        db: Database connection
        event_ring: Where open pages hear about resolved alerts
        settings_service: Current settings
        cursors: The deep walk's positions, kept across ticks by the timer;
            None starts every folder from the top

    Returns:
        (messages marked read, alerts resolved)
    """
    cursors = {} if cursors is None else cursors
    roles = always_read_roles(settings_service)
    async with db.session() as session:
        account_ids = (
            await session.execute(text("SELECT id FROM accounts WHERE is_active"))
        ).scalars().all()

    repo = FolderRepository(db)
    marked = 0
    for account_id in account_ids:
        for role in roles:
            folder_id = await repo.resolve_special_folder(account_id, role)
            if folder_id is not None:
                marked += await _reconcile_folder(db, folder_id, cursors)

    async with db.session() as session:
        resolved = await resolve_all(session)
    if resolved:
        await announce_alerts_dismissed(db, event_ring)

    if marked or resolved:
        logger.info("Read state reconcile", extra={"marked": marked, "resolved": len(resolved)})
    return marked, len(resolved)


def build_read_state_timer(
    db: DatabaseConnection, event_ring: EventRing | None, settings_service: SettingsService,
) -> ReconciliationTimer:
    """The advisory-locked periodic reconcile -- one per process, safe
    with more than one replica."""
    cursors: Cursors = {}

    async def _callback() -> None:
        await reconcile_read_state_once(db, event_ring, settings_service, cursors)

    return ReconciliationTimer(db, _RECONCILE_LOCK_KEY, _callback, _RECONCILE_INTERVAL_SECONDS)
