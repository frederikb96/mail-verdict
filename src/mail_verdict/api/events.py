"""
Server-Sent Events (SSE) endpoint for real-time updates.

GET /api/events — SSE stream with Last-Event-ID replay support.
Supports ?account_id=<uuid> query parameter to filter events by account.

On fresh connect: sends connected event, then streams live events.
On reconnect (Last-Event-ID header): replays missed events from EventRing,
falls back to connected event if the ID is too old.

PostIMAP integration: PG LISTEN/NOTIFY (via pg_listener.py) pushes
message events into the EventRing. The SSE subscriber on the event bus
is no longer needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from mail_verdict.api.event_ring import EventRing

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


async def push_selection_event(
    account_id: uuid.UUID,
    selected_ids: set[uuid.UUID],
    count: int,
) -> None:
    """
    Push a selection.changed event into the EventRing.

    Args:
        account_id: Account UUID
        selected_ids: Currently selected message IDs
        count: Number of selected messages
    """
    if _event_ring is None:
        return

    await _event_ring.add(
        account_id=account_id,
        event_type="selection.changed",
        data={
            "account_id": str(account_id),
            "selected_ids": [str(mid) for mid in selected_ids],
            "count": count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


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
        if last_event_id is not None:
            # Reconnect: try to replay from ring
            if event_ring.has_events_after(last_event_id, account_id):
                missed = await event_ring.replay_from(last_event_id, account_id)
                for event in missed:
                    yield _format_sse(event["id"], event["event_type"], event["data"])
            else:
                # Gap too large: send connected event
                seq = event_ring.get_latest_seq()
                yield f"id: {seq}\nevent: connected\ndata: {{}}\n\n"
        else:
            # Fresh connect: send connected event
            seq = event_ring.get_latest_seq()
            yield f"id: {seq}\nevent: connected\ndata: {{}}\n\n"

        # Stream live events
        last_seen = event_ring.get_latest_seq()
        while True:
            if await request.is_disconnected():
                return

            waiter.clear()
            try:
                await asyncio.wait_for(waiter.wait(), timeout=KEEPALIVE_INTERVAL_S)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue

            # Fetch new events since last_seen
            new_events = await event_ring.replay_from(last_seen, account_id)
            for event in new_events:
                yield _format_sse(event["id"], event["event_type"], event["data"])
                last_seen = event["id"]

    except asyncio.CancelledError:
        return
    finally:
        event_ring.unregister_waiter(waiter, account_id)


def _validate_api_key(request: Request) -> bool:
    """
    Validate API key from X-API-Key header or query parameter.

    Mirrors the FastAPI require_auth dependency but works with raw Starlette
    requests (SSE route bypasses FastAPI middleware stack).

    Args:
        request: Starlette request

    Returns:
        True if auth passes (key valid or auth disabled)
    """
    expected = os.environ.get("MAIL_VERDICT_API_KEY")
    if not expected:
        return True
    api_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if not api_key:
        return False
    return secrets.compare_digest(api_key, expected)


async def sse_endpoint(request: Request) -> StreamingResponse | JSONResponse:
    """
    SSE endpoint handler.

    Validates API key before streaming (this route bypasses FastAPI's
    dependency injection since it's mounted as a raw Starlette Route).

    Supports ?account_id=<uuid> for per-account filtering.
    Supports Last-Event-ID header (auto-reconnect) and ?last_event_id query
    parameter (manual reconnect) for replay.
    """
    if not _validate_api_key(request):
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key"},
        )

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
            uuid.UUID(raw_account_id)
            filter_account_id = raw_account_id
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
