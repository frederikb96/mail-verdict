"""
Server-Sent Events (SSE) endpoint for real-time updates.

GET /api/events — SSE stream with Last-Event-ID replay support.
Supports ?account_id=<uuid> query parameter to filter events by account.

On fresh connect: sends connected event, then streams live events.
On reconnect (Last-Event-ID header): replays missed events from EventRing,
falls back to a resync event -- telling the client to invalidate every
cache rather than trust one that may now be stale -- if the ID is too old
to replay or was issued by another process (see EventRing.epoch).

PostIMAP integration: postimap/listener.py listens on postimap_events and
pushes parsed events into the EventRing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from mail_verdict.database.models import Account

if TYPE_CHECKING:
    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

# Every event name this stream can carry. EventRing.add refuses any other, and
# tests/unit/test_sse_event_types.py checks every name the source emits against
# it, so a new event is a registry entry -- and a diff of the exported contract
# (docs/api-contract/sse-events.json) -- before it can reach a client.
SSE_EVENT_TYPES: frozenset[str] = frozenset({
    "account.changed",
    "alert.dismissed",
    "alert.new",
    "calendar.account",
    "calendar.collection",
    "calendar.links_changed",
    "calendar.object",
    "connected",
    "contact.collection",
    "contact.object",
    "folder.changed",
    "folder.synced",
    "identity.changed",
    "mail.deleted",
    "mail.new",
    "mail.updated",
    "notification.new",
    "outbox.updated",
    "pipeline.document_changed",
    "pipeline.notify",
    "pipeline.run_finished",
    "resync",
    "settings.changed",
    "verdict.issued",
})

# Global EventRing instance (set during lifespan)
_event_ring: EventRing | None = None

KEEPALIVE_INTERVAL_S = 15


def init_event_ring(ring: EventRing) -> None:
    """
    Set the global EventRing instance.

    Called once during server lifespan.

    Args:
        ring: EventRing to use for SSE events
    """
    global _event_ring
    _event_ring = ring


def get_event_ring() -> EventRing | None:
    """Get the global EventRing instance."""
    return _event_ring


async def push_verdict_event(
    mail_id: uuid.UUID,
    is_spam: bool,
    source: str,
    account_id: uuid.UUID | None = None,
) -> None:
    """
    Push a verdict_issued event into the EventRing.

    Args:
        mail_id: Message UUID
        is_spam: Spam classification result
        source: Verdict source identifier
        account_id: Optional account UUID for scoping
    """
    if _event_ring is None or account_id is None:
        return

    await _event_ring.add(
        account_id=account_id,
        event_type="verdict.issued",
        data={
            "message_id": str(mail_id),
            "is_spam": is_spam,
            "source": source,
            "account_id": str(account_id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


async def broadcast_event(
    db: DatabaseConnection, event_ring: EventRing, event_type: str, data: dict[str, Any],
) -> None:
    """
    Push one event to every account's ring.

    EventRing keys everything by account_id, but some of what MailVerdict
    itself owns is not account-scoped at all -- the pipeline document and
    the global settings categories are each one row shared by the whole
    instance, not one per account. The browser's own SSE connection has
    no account filter either (useSSE() is called with none), so any one
    account's ring reaches it; broadcasting to all of them is what stays
    correct if a per-account filtered connection is ever added later.

    Args:
        db: Database connection to read the account list from
        event_ring: Ring buffer to push the event into
        event_type: SSE event type
        data: Payload to send with every copy of the event
    """
    async with db.session() as session:
        account_ids = (await session.execute(select(Account.id))).scalars().all()
    for account_id in account_ids:
        await event_ring.add(account_id, event_type, data)


async def broadcast_resync(db: DatabaseConnection, event_ring: EventRing) -> None:
    """
    Push a resync event to every account.

    The postimap_events listener's own reconnect calls this once a
    connection loss ends: any NOTIFY fired during the gap is gone for
    good, so every currently connected client needs the same "invalidate
    everything" signal a browser reconnecting with a stale Last-Event-ID
    already gets from `_sse_generator`'s own gap-detection fallback --
    this is what reaches the ones whose own SSE connection never dropped
    at all, and would otherwise keep showing what they had before the gap.

    Args:
        db: Database connection to read the account list from
        event_ring: Ring buffer to push the event into
    """
    await broadcast_event(db, event_ring, "resync", {})


# What sse_endpoint passes as the Last-Event-ID of a client presenting an id
# this ring never issued -- a previous process's, most commonly. There is
# nothing to replay it from, so the generator answers with a resync.
FOREIGN_EVENT_ID = -1


def _format_sse(event_id: str, event_type: str, data: dict[str, Any]) -> str:
    """
    Format an SSE message with id, event type, and JSON data.

    Args:
        event_id: The ring's formatted id, for Last-Event-ID tracking
        event_type: SSE event type
        data: Payload to serialize as JSON

    Returns:
        SSE-formatted string
    """
    return f"id: {event_id}\nevent: {event_type}\ndata: {json.dumps(data)}\n\n"


async def _sse_generator(
    event_ring: EventRing,
    account_id: str | None,
    last_event_id: int | None,
    request: Request,
) -> AsyncGenerator[str, None]:
    """
    Async generator yielding SSE-formatted strings from the EventRing.

    On first connect (no Last-Event-ID): sends connected event, then live.
    On reconnect (with Last-Event-ID): replays missed events, then live.
    Sends keepalive every 15s.

    Args:
        event_ring: Ring buffer to read events from
        account_id: Optional account filter
        last_event_id: Last-Event-ID from reconnecting client
        request: Starlette request for disconnect detection
    """
    waiter = event_ring.register_waiter(account_id)
    try:
        last_seen: int
        if last_event_id is not None:
            # Reconnect: replay from the ring when every id since the
            # client's is still in it -- never for an id this ring did not
            # issue (FOREIGN_EVENT_ID) or one past its own counter, which
            # only a previous process could have handed out.
            replayable = (
                0 <= last_event_id <= event_ring.get_latest_seq()
                and event_ring.has_events_after(last_event_id, account_id)
            )
            if replayable:
                missed = await event_ring.replay_from(last_event_id, account_id)
                for event in missed:
                    yield _format_sse(
                        event_ring.format_event_id(event["id"]), event["event_type"], event["data"],
                    )
                # last_seen is what this client was actually handed, never a
                # fresh read of the global counter -- anything appended to
                # the ring while the yields above were suspended would
                # otherwise get an id at or below a post-yield read and be
                # skipped for good, since the next replay starts past it.
                last_seen = missed[-1]["id"] if missed else last_event_id
            else:
                # Gap too large to replay, or an id from another process:
                # whatever changed while disconnected is not in this ring,
                # so tell the client to invalidate everything rather than
                # trust a cache that may be stale.
                seq = event_ring.get_latest_seq()
                yield _format_sse(event_ring.format_event_id(seq), "resync", {})
                last_seen = seq
        else:
            # Fresh connect: send connected event. Its id is what the client
            # reconnects with, so a gap is replayable even when no other
            # event reached it first.
            seq = event_ring.get_latest_seq()
            yield _format_sse(event_ring.format_event_id(seq), "connected", {})
            last_seen = seq

        # Stream live events
        while True:
            if await request.is_disconnected():
                return

            # Clear before checking the ring, not after: an event that
            # arrives between the clear and the check is still caught by
            # replay_from below, and the wait is only entered once the ring
            # is confirmed to have nothing new -- so a wake landing while
            # this generator is mid-yield is never discarded outright, only
            # ever picked up a little later than it could have been.
            waiter.clear()
            new_events = await event_ring.replay_from(last_seen, account_id)
            if not new_events:
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=KEEPALIVE_INTERVAL_S)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                new_events = await event_ring.replay_from(last_seen, account_id)

            for event in new_events:
                yield _format_sse(
                    event_ring.format_event_id(event["id"]), event["event_type"], event["data"],
                )
                last_seen = event["id"]

    except asyncio.CancelledError:
        return
    finally:
        event_ring.unregister_waiter(waiter, account_id)


async def sse_endpoint(request: Request) -> StreamingResponse | JSONResponse:
    """
    SSE endpoint handler.

    Supports ?account_id=<uuid> for per-account filtering.
    Supports Last-Event-ID header (auto-reconnect) and ?last_event_id query
    parameter (manual reconnect) for replay.
    """
    if _event_ring is None:
        return StreamingResponse(
            iter([": server not ready\n\n"]),
            media_type="text/event-stream",
            status_code=503,
        )

    # Parse optional account_id filter
    filter_account_id: str | None = None
    raw_account_id = request.query_params.get("account_id")
    if raw_account_id:
        try:
            # Canonical (lowercase, hyphenated) form -- EventRing keys its
            # per-account rings on str(uuid.UUID), so keeping the client's
            # raw casing here would never match, and the client would see
            # nothing but keepalives with no error anywhere.
            filter_account_id = str(uuid.UUID(raw_account_id))
        except ValueError:
            pass

    # Parse Last-Event-ID from header (auto-reconnect) or query param (manual reconnect)
    last_event_id: int | None = None
    raw_last_id = (
        request.headers.get("Last-Event-ID")
        or request.headers.get("last-event-id")
        or request.query_params.get("last_event_id")
    )
    if raw_last_id:
        parsed = _event_ring.parse_event_id(raw_last_id)
        last_event_id = FOREIGN_EVENT_ID if parsed is None else parsed

    return StreamingResponse(
        _sse_generator(_event_ring, filter_account_id, last_event_id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
