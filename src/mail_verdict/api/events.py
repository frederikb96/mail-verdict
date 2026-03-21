"""
Server-Sent Events (SSE) endpoint for real-time updates.

GET /api/events — SSE stream with Last-Event-ID replay support.
Supports ?account_id=<uuid> query parameter to filter events by account.

On fresh connect: sends sync.state snapshot, then streams live events.
On reconnect (Last-Event-ID header): replays missed events from EventRing,
falls back to full snapshot if the ID is too old.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import StreamingResponse

from mail_verdict.api.event_ring import EventRing
from mail_verdict.rules.bus import EventBus, Subscriber
from mail_verdict.sync.events import (
    FlagsChanged,
    MailDeleted,
    MailMoved,
    MailReceived,
    MailSpamDetected,
    MailTrashed,
    SyncEvent,
)

logger = logging.getLogger(__name__)

# Global EventRing instance (set during lifespan)
_event_ring: EventRing | None = None
_subscriber_registered = False

# Tracker accessor (set during lifespan, avoids circular import)
_get_tracker: Any = None

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


def set_tracker_accessor(accessor: Any) -> None:
    """
    Register a callable that retrieves a SyncTracker by account_id.

    Avoids circular imports between api.events and sync.engine.

    Args:
        accessor: Callable(account_id: uuid.UUID) -> SyncTracker | None
    """
    global _get_tracker
    _get_tracker = accessor


async def register_sse_subscriber(bus: EventBus) -> None:
    """
    Register event bus subscriber to push mail events into the EventRing.

    Called once during server lifespan. Mail events (new, deleted, moved,
    flags changed) are converted and added to the ring buffer.
    """
    global _subscriber_registered
    if _subscriber_registered:
        return

    async def _dispatch_to_ring(event: SyncEvent) -> None:
        """Forward event bus events into the EventRing."""
        if _event_ring is None:
            return

        event_type, data = _sync_event_to_sse(event)
        if event_type is None:
            return

        _event_ring.add(
            account_id=event.account_id,
            event_type=event_type,
            data=data,
        )

    event_types = [
        MailReceived,
        MailDeleted,
        MailMoved,
        MailTrashed,
        MailSpamDetected,
        FlagsChanged,
    ]
    for et in event_types:
        await bus.subscribe(
            et,
            Subscriber(name="sse_ring_broadcaster", callback=_dispatch_to_ring, priority=90),
        )

    _subscriber_registered = True
    logger.info("SSE ring broadcaster registered on event bus")


def _sync_event_to_sse(event: SyncEvent) -> tuple[str | None, dict[str, Any]]:
    """
    Convert a SyncEvent to an SSE event type and payload.

    Args:
        event: Sync event from event bus

    Returns:
        Tuple of (event_type, data) or (None, {}) if unmapped
    """
    event_name = type(event).__name__

    type_map = {
        "MailReceived": "mail.new",
        "MailMoved": "mail.updated",
        "MailTrashed": "mail.updated",
        "MailSpamDetected": "mail.updated",
        "MailDeleted": "mail.deleted",
        "FlagsChanged": "mail.updated",
    }

    sse_type = type_map.get(event_name)
    if sse_type is None:
        return None, {}

    data: dict[str, Any] = {
        "account_id": str(event.account_id),
        "folder_id": str(event.folder_id),
        "timestamp": event.timestamp.isoformat(),
    }

    if hasattr(event, "uid"):
        data["uid"] = event.uid
    if hasattr(event, "message_id") and event.message_id:  # type: ignore[union-attr]
        data["message_id"] = event.message_id  # type: ignore[union-attr]
    if hasattr(event, "is_read"):
        data["is_read"] = event.is_read  # type: ignore[union-attr]
    if hasattr(event, "is_flagged"):
        data["is_flagged"] = event.is_flagged  # type: ignore[union-attr]

    return sse_type, data


async def push_verdict_event(
    mail_id: uuid.UUID,
    is_spam: bool,
    source: str,
    account_id: uuid.UUID | None = None,
) -> None:
    """
    Push a verdict_issued event into the EventRing.

    Args:
        mail_id: Mail UUID
        is_spam: Spam classification result
        source: Verdict source identifier
        account_id: Optional account UUID for scoping
    """
    if _event_ring is None or account_id is None:
        return

    _event_ring.add(
        account_id=account_id,
        event_type="verdict.issued",
        data={
            "mail_id": str(mail_id),
            "is_spam": is_spam,
            "source": source,
            "account_id": str(account_id),
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

    On first connect (no Last-Event-ID): sends sync.state snapshot, then live.
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
                # Gap too large: send full state snapshot
                for msg in _emit_state_snapshot(event_ring, account_id):
                    yield msg
        else:
            # Fresh connect: send current state snapshot
            for msg in _emit_state_snapshot(event_ring, account_id):
                yield msg

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


def _emit_state_snapshot(
    event_ring: EventRing,
    account_id: str | None,
) -> list[str]:
    """
    Build sync.state snapshot messages for connected accounts.

    Args:
        event_ring: Ring buffer (used for sequence ID)
        account_id: Optional account filter

    Returns:
        List of SSE-formatted strings with state snapshots
    """
    messages: list[str] = []
    seq = event_ring.get_latest_seq()

    if _get_tracker is None:
        return messages

    if account_id:
        try:
            acct_uuid = uuid.UUID(account_id)
        except ValueError:
            return messages
        tracker = _get_tracker(acct_uuid)
        if tracker:
            messages.append(_format_sse(seq, "sync.state", tracker.to_dict()))
    else:
        # Would need access to all trackers — for now just send the seq
        # as a keepalive so the client knows the connection is alive
        messages.append(f"id: {seq}\nevent: connected\ndata: {{}}\n\n")

    return messages


async def sse_endpoint(request: Request) -> StreamingResponse:
    """
    SSE endpoint handler.

    Supports ?account_id=<uuid> for per-account filtering.
    Supports Last-Event-ID header for reconnect replay.
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
            uuid.UUID(raw_account_id)
            filter_account_id = raw_account_id
        except ValueError:
            pass

    # Parse Last-Event-ID header
    last_event_id: int | None = None
    raw_last_id = request.headers.get("Last-Event-ID") or request.headers.get("last-event-id")
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
