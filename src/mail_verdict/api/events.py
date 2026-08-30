"""
Server-Sent Events (SSE) endpoint for real-time updates.

GET /api/events — SSE stream with Last-Event-ID replay support.
Supports ?account_id=<uuid> query parameter to filter events by account.

On fresh connect: sends connected event, then streams live events.
On reconnect (Last-Event-ID header): replays missed events from EventRing,
falls back to a resync event -- telling the client to invalidate every
cache rather than trust one that may now be stale -- if the ID is too old
to replay.

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

from mail_verdict.api.event_ring import EventRing
from mail_verdict.database.models import Account

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

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
    async with db.session() as session:
        account_ids = (await session.execute(select(Account.id))).scalars().all()
    for account_id in account_ids:
        await event_ring.add(account_id, "resync", {})


def _format_sse(event_id: int, event_type: str, data: dict[str, Any]) -> str:
    """
    Format an SSE message with id, event type, and JSON data.

    Args:
        event_id: Sequence ID for Last-Event-ID tracking
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
            # Reconnect: try to replay from ring
            if event_ring.has_events_after(last_event_id, account_id):
                missed = await event_ring.replay_from(last_event_id, account_id)
                for event in missed:
                    yield _format_sse(event["id"], event["event_type"], event["data"])
                # last_seen is what this client was actually handed, never a
                # fresh read of the global counter -- anything appended to
                # the ring while the yields above were suspended would
                # otherwise get an id at or below a post-yield read and be
                # skipped for good, since the next replay starts past it.
                last_seen = missed[-1]["id"] if missed else last_event_id
            else:
                # Gap too large to replay: whatever changed while disconnected
                # is gone from the ring, so tell the client to invalidate
                # everything rather than trust a cache that may be stale.
                seq = event_ring.get_latest_seq()
                yield f"id: {seq}\nevent: resync\ndata: {{}}\n\n"
                last_seen = seq
        else:
            # Fresh connect: send connected event
            seq = event_ring.get_latest_seq()
            yield f"id: {seq}\nevent: connected\ndata: {{}}\n\n"
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
                yield _format_sse(event["id"], event["event_type"], event["data"])
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
        try:
            last_event_id = int(raw_last_id)
        except ValueError:
            pass

    return StreamingResponse(
        _sse_generator(_event_ring, filter_account_id, last_event_id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
