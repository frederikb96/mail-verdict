"""
A mail alert resolves itself -- dismissed_at stamped, exactly as a click
on it would -- once the mail it announced is read, by whatever path:
opened here, marked read here or in another client, or marked read on
landing in Archive or Trash (filing/read_state.py).

"The mail it announced" is the alert's own message row, or any live row
in the same account carrying the same Message-ID header. The second half
is what follows a move made in another mail client: PostIMAP mirrors that
as an expunge in the source folder plus a fresh row in the destination
(consumer contract, "Moving a message"), so the alert still names the
expunged source row and only the header reaches the row that is read.

_RESOLVED is that predicate, written once. Every entry point below
differs only in which alerts it considers: the ones reachable from a
message that just changed (the postimap event path), the staged ones
about to be delivered, a single alert just created, or every unresolved
one (the periodic sweep that catches an event the listener missed).

A staged alert resolved before it was ever delivered gets delivered_at
stamped along with dismissed_at: it leaves the dispatch queue without
being announced, and its row stays as the dedupe record that keeps a
later resync of the same message from alerting again.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from mail_verdict.api.events import broadcast_event
from mail_verdict.push.relay import get_relay_client_if_ready

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.database.connection import DatabaseConnection


def _same_mail(row: str, origin: str) -> str:
    """SQL: `row` is the same mail as `origin` -- the same row, or a row in
    the same account with the same Message-ID header. A NULL header
    compares as unknown, so headerless mail only ever matches itself."""
    return (
        f"({row}.account_id = {origin}.account_id"
        f" AND ({row}.id = {origin}.id OR {row}.message_id = {origin}.message_id))"
    )


_RESOLVED = f"""
    EXISTS (
        SELECT 1 FROM messages m
        WHERE {_same_mail("m", "origin")}
          AND m.is_seen
          AND m.expunged_at IS NULL
    )
"""


async def _resolve(session: AsyncSession, scope: str, params: dict[str, Any]) -> list[uuid.UUID]:
    """Stamp every unresolved mail alert within `scope` whose mail is read.

    Args:
        session: Active AsyncSession (caller commits)
        scope: Extra SQL condition on `a` narrowing which alerts are
            considered -- one of the fixed fragments below, never
            caller-supplied text
        params: Bind parameters `scope` refers to

    Returns:
        The ids of the alerts this call resolved
    """
    result = await session.execute(
        text(
            f"""
            UPDATE alerts a
            SET dismissed_at = now(), delivered_at = coalesce(a.delivered_at, now())
            FROM messages origin
            WHERE a.kind = 'mail'
              AND a.dismissed_at IS NULL
              AND origin.id = a.message_id
              AND ({scope})
              AND {_RESOLVED}
            RETURNING a.id
            """
        ),
        params,
    )
    return list(result.scalars().all())


async def resolve_for_messages(
    session: AsyncSession, message_ids: list[uuid.UUID],
) -> list[uuid.UUID]:
    """Resolve the alerts of whichever mail these message rows are -- the
    rows themselves, or any row sharing a header with one of them."""
    if not message_ids:
        return []
    scope = f"""
        a.message_id IN (
            SELECT origin2.id FROM messages src
            JOIN messages origin2 ON {_same_mail("origin2", "src")}
            WHERE src.id = ANY(:message_ids)
        )
    """
    return await _resolve(session, scope, {"message_ids": message_ids})


async def resolve_staged(session: AsyncSession) -> list[uuid.UUID]:
    """Resolve staged alerts whose mail was read before delivery."""
    return await _resolve(session, "a.delivered_at IS NULL", {})


async def resolve_alert(session: AsyncSession, alert_id: uuid.UUID) -> bool:
    """Resolve one alert if its mail is already read. Returns whether it did."""
    return bool(await _resolve(session, "a.id = :alert_id", {"alert_id": alert_id}))


async def resolve_all(session: AsyncSession) -> list[uuid.UUID]:
    """Resolve every unresolved mail alert whose mail is read. Unbatched:
    the unresolved set is what the bell is showing, which this very
    mechanism keeps down to mail nobody has read yet."""
    return await _resolve(session, "true", {})


async def announce_alerts_dismissed(db: DatabaseConnection, event_ring: EventRing | None) -> None:
    """alert.dismissed to every open page, so a bell showing an alert that
    was just dismissed or resolved drops it without polling -- and a
    throttled silent push to every native device, which has no open
    stream to hear it on (push/relay.py's read-sync)."""
    if event_ring is not None:
        await broadcast_event(db, event_ring, "alert.dismissed", {})
    relay = get_relay_client_if_ready()
    if relay is not None:
        relay.schedule_read_sync(db)


async def resolve_for_message_event(
    db: DatabaseConnection, event_ring: EventRing | None, message_id: str,
) -> list[uuid.UUID]:
    """The postimap event path: a message row was inserted or its read
    state changed -- resolve whatever alert that settles, and announce it.

    Args:
        db: Database connection
        event_ring: Where open pages are told; None skips the announcement
        message_id: The event's own `id`, the messages.id that changed

    Returns:
        The ids of the alerts resolved
    """
    try:
        message_uuid = uuid.UUID(message_id)
    except ValueError:
        return []
    async with db.session() as session:
        resolved = await resolve_for_messages(session, [message_uuid])
    if resolved:
        await announce_alerts_dismissed(db, event_ring)
    return resolved
