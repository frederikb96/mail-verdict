"""
Server-Sent Events (SSE) endpoint for real-time updates.

GET /api/events — SSE stream for new mail, verdicts, folder changes, sync status.
Supports ?account_id=<uuid> query parameter to filter events by account.
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

# Maps each SSE client queue to its optional account_id filter (None = all events)
_sse_clients: dict[asyncio.Queue[dict[str, Any]], str | None] = {}
_subscriber_registered = False


async def register_sse_subscriber(bus: EventBus) -> None:
    """
    Register a single subscriber on the EventBus to fan out events to all SSE clients.

    Called once during server lifespan.
    """
    global _subscriber_registered
    if _subscriber_registered:
        return

    async def _dispatch_to_sse(event: SyncEvent) -> None:
        """Forward event bus events to connected SSE clients, filtering by account."""
        sse_event = _event_to_sse(event)
        if sse_event is None:
            return

        event_account_id = sse_event.get("account_id")
        dead_queues: list[asyncio.Queue[dict[str, Any]]] = []

        for queue, filter_account_id in _sse_clients.items():
            if filter_account_id is not None and filter_account_id != event_account_id:
                continue
            try:
                queue.put_nowait(sse_event)
            except asyncio.QueueFull:
                dead_queues.append(queue)

        for q in dead_queues:
            _sse_clients.pop(q, None)

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
            Subscriber(name="sse_broadcaster", callback=_dispatch_to_sse, priority=90),
        )

    _subscriber_registered = True
    logger.info("SSE broadcaster registered on event bus")


def _event_to_sse(event: SyncEvent) -> dict[str, Any] | None:
    """Convert a SyncEvent to an SSE-compatible dict."""
    event_name = type(event).__name__

    type_map = {
        "MailReceived": "new_mail",
        "MailMoved": "folder_change",
        "MailTrashed": "folder_change",
        "MailSpamDetected": "folder_change",
        "MailDeleted": "folder_change",
        "FlagsChanged": "flags_changed",
    }

    sse_type = type_map.get(event_name)
    if sse_type is None:
        return None

    data: dict[str, Any] = {
        "event": sse_type,
        "detail": event_name,
        "account_id": str(event.account_id),
        "folder_id": str(event.folder_id),
        "timestamp": event.timestamp.isoformat(),
    }

    if hasattr(event, "uid"):
        data["uid"] = event.uid
    if hasattr(event, "message_id") and event.message_id:  # type: ignore[union-attr]
        data["message_id"] = event.message_id  # type: ignore[union-attr]

    return data


async def push_verdict_event(
    mail_id: uuid.UUID,
    is_spam: bool,
    source: str,
    account_id: uuid.UUID | None = None,
) -> None:
    """Push a verdict_issued event to SSE clients (filtered by account if set)."""
    sse_event: dict[str, Any] = {
        "event": "verdict_issued",
        "mail_id": str(mail_id),
        "is_spam": is_spam,
        "source": source,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if account_id:
        sse_event["account_id"] = str(account_id)

    event_account_str = sse_event.get("account_id")
    for queue, filter_account_id in _sse_clients.items():
        if filter_account_id is not None and filter_account_id != event_account_str:
            continue
        try:
            queue.put_nowait(sse_event)
        except asyncio.QueueFull:
            pass


async def push_sync_status(
    account_id: uuid.UUID,
    status: str,
) -> None:
    """Push a sync_status event to SSE clients (filtered by account if set)."""
    sse_event = {
        "event": "sync_status",
        "account_id": str(account_id),
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    event_account_str = str(account_id)
    for queue, filter_account_id in _sse_clients.items():
        if filter_account_id is not None and filter_account_id != event_account_str:
            continue
        try:
            queue.put_nowait(sse_event)
        except asyncio.QueueFull:
            pass


async def _sse_generator(
    queue: asyncio.Queue[dict[str, Any]],
) -> AsyncGenerator[str, None]:
    """
    Async generator that yields SSE-formatted strings from the queue.

    Sends a keepalive comment every 30s to prevent connection timeout.
    """
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                event_type = event.get("event", "message")
                data = json.dumps(event)
                yield f"event: {event_type}\ndata: {data}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    except asyncio.CancelledError:
        return


async def sse_endpoint(request: Request) -> StreamingResponse:
    """SSE endpoint handler. Supports ?account_id=<uuid> for per-account filtering."""
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)

    # Parse optional account_id filter from query params
    filter_account_id: str | None = None
    raw_account_id = request.query_params.get("account_id")
    if raw_account_id:
        try:
            uuid.UUID(raw_account_id)
            filter_account_id = raw_account_id
        except ValueError:
            pass

    _sse_clients[queue] = filter_account_id

    async def event_stream() -> AsyncGenerator[str, None]:
        """Stream wrapper that cleans up on disconnect."""
        try:
            async for chunk in _sse_generator(queue):
                if await request.is_disconnected():
                    break
                yield chunk
        finally:
            _sse_clients.pop(queue, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
