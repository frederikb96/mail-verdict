"""
Server-Sent Events (SSE) endpoint for real-time updates.

GET /api/events — SSE stream with Last-Event-ID replay support.

A stream carries the events of the caller's own ring keys and nothing else
(Scope.ring_keys: their mail and DAV accounts, their user key, the admin
key for an admin, and the key every stream receives). ?account_id=<uuid>
narrows the account keys to that one account, if it is the caller's; the
caller's user, admin and everyone keys stay. The keys are resolved again
whenever an account changes owner or a user's role or sign-in changes
(auth/ownership.py), so an account added, removed or given away mid-stream
starts or stops reaching it at once, and at least every
auth.principal_cache_seconds besides. A stream whose user has been disabled
ends at that point; the client's reconnect is then refused by the gate.

On fresh connect: sends connected event, then streams live events.
On reconnect (Last-Event-ID header): replays missed events of the caller's
keys from EventRing, falls back to a resync event -- telling the client to
invalidate every cache rather than trust one that may now be stale -- if
the ID is too old to replay or was issued by another process (see
EventRing.epoch).

PostIMAP integration: postimap/listener.py listens on postimap_events and
pushes parsed events into the EventRing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from mail_verdict.auth.ownership import on_ownership_change, ownership_generation
from mail_verdict.auth.principal import OWNER_USER_ID, Principal, current_principal
from mail_verdict.auth.scope import EVERYONE_RING_KEY, Scope
from mail_verdict.config import get_config
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import User

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
    "order.updated",
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

# How often an idle stream carries a heartbeat. It is a named `ping` event
# rather than a comment line because EventSource hides comments from the page,
# so a browser could not tell a quiet stream from a dead one. It is not a ring
# event: it has no id (a client's Last-Event-ID stays what it was) and is not
# in SSE_EVENT_TYPES.
KEEPALIVE_INTERVAL_S = 15
PING_FRAME = "event: ping\ndata: {}\n\n"

# Apple's URLSession holds back a streamed response -- the response callback
# and the body alike -- until it has 512 bytes, unless the content type is
# one it trusts (JSON). A reconnect with nothing to replay sends no bytes at
# all and a fresh one a single short `connected` frame, so an iOS client
# would sit "connecting" until enough pings piled up, minutes later. A
# comment this size, sent before anything else, pushes every stream past
# that threshold at once; EventSource and every SSE parser ignore comments.
STREAM_PREAMBLE = ":" + " " * 2048 + "\n\n"


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


def _wake_streams() -> None:
    if _event_ring is not None:
        _event_ring.wake_all()


on_ownership_change(_wake_streams)


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
        account_id,
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
    Push one event to every open stream, whoever's it is.

    Only for an event that carries nothing of anyone's: a resync, or a bare
    "something of this kind changed" every client answers by refetching its
    own view. Anything naming a user's data goes on that user's key.

    Args:
        db: Not read: nothing about who receives it needs looking up
        event_ring: Ring buffer to push the event into
        event_type: SSE event type
        data: Payload, the same for every stream
    """
    await event_ring.add(EVERYONE_RING_KEY, event_type, data)


async def broadcast_resync(db: DatabaseConnection, event_ring: EventRing) -> None:
    """
    Push a resync event to every open stream.

    The postimap_events listener's own reconnect calls this once a
    connection loss ends: any NOTIFY fired during the gap is gone for
    good, so every currently connected client needs the same "invalidate
    everything" signal a browser reconnecting with a stale Last-Event-ID
    already gets from `_sse_generator`'s own gap-detection fallback --
    this is what reaches the ones whose own SSE connection never dropped
    at all, and would otherwise keep showing what they had before the gap.

    Args:
        db: Database connection
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


# Resolves the ring keys a stream may read, as of now. Raises StreamClosed
# when the stream may no longer read anything at all.
KeyResolver = Callable[[], Awaitable[frozenset[str]]]


class StreamClosed(Exception):
    """The caller of a stream may no longer stream: their user is disabled."""


async def _sse_generator(
    event_ring: EventRing,
    resolve_keys: KeyResolver,
    last_event_id: int | None,
    request: Request,
    *,
    recheck_seconds: float,
) -> AsyncGenerator[str, None]:
    """
    Async generator yielding SSE-formatted strings from the EventRing.

    On first connect (no Last-Event-ID): sends connected event, then live.
    On reconnect (with Last-Event-ID): replays missed events, then live.
    Sends a `ping` heartbeat event every KEEPALIVE_INTERVAL_S while idle.
    Only ever reads the keys `resolve_keys` returns, asked again before the
    next read whenever ownership_generation() has moved or `recheck_seconds`
    have passed since it was last asked; ends as soon as it raises
    StreamClosed.

    Args:
        event_ring: Ring buffer to read events from
        resolve_keys: The keys this stream may read
        last_event_id: Last-Event-ID from reconnecting client
        request: Starlette request for disconnect detection
        recheck_seconds: The longest the keys are trusted without asking
            again -- what bounds a change no generation bump announced
    """
    generation = ownership_generation()
    try:
        keys = await resolve_keys()
    except StreamClosed:
        return
    resolved_at = time.monotonic()
    waiter = event_ring.register_waiter(keys)
    try:
        last_seen: int
        if last_event_id is not None:
            # Reconnect: replay from the ring when every id since the
            # client's is still in it -- never for an id this ring did not
            # issue (FOREIGN_EVENT_ID) or one past its own counter, which
            # only a previous process could have handed out.
            replayable = (
                0 <= last_event_id <= event_ring.get_latest_seq()
                and event_ring.has_events_after(last_event_id, keys)
            )
            if replayable:
                missed = await event_ring.replay_from(last_event_id, keys)
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

        async def _current_keys() -> frozenset[str]:
            # Ownership or the user changed since the keys were read, or they
            # are older than recheck_seconds: read them again before the
            # ring, so an account given away is never read once more, one
            # just added is read from here on, and a disabled user's stream
            # ends (StreamClosed, caught below).
            nonlocal generation, keys, waiter, resolved_at
            stale = time.monotonic() - resolved_at >= recheck_seconds
            if stale or generation != ownership_generation():
                generation = ownership_generation()
                resolved_at = time.monotonic()
                fresh = await resolve_keys()
                if fresh != keys:
                    event_ring.unregister_waiter(waiter, keys)
                    keys = fresh
                    waiter = event_ring.register_waiter(keys)
            return keys

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
            new_events = await event_ring.replay_from(last_seen, await _current_keys())
            if not new_events:
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=KEEPALIVE_INTERVAL_S)
                except asyncio.TimeoutError:
                    yield PING_FRAME
                    continue
                new_events = await event_ring.replay_from(last_seen, await _current_keys())

            for event in new_events:
                yield _format_sse(
                    event_ring.format_event_id(event["id"]), event["event_type"], event["data"],
                )
                last_seen = event["id"]

    except (asyncio.CancelledError, StreamClosed):
        return
    finally:
        event_ring.unregister_waiter(waiter, keys)


async def _scope_of(principal: Principal) -> Scope:
    """
    The scope a stream of `principal` reads, as of now: read from the users
    table rather than the principal, so a role changed since the stream
    opened counts, and a user disabled since then closes it.

    Raises:
        StreamClosed: The user is disabled (the owner never is).
    """
    db = get_db_connection()
    if principal.user_id != OWNER_USER_ID:
        async with db.session() as session:
            disabled = await session.scalar(
                select(User.disabled).where(User.id == principal.user_id)
            )
        if disabled is not False:
            raise StreamClosed()
    return await Scope.for_user(db, principal.user_id)


def _recheck_seconds() -> float:
    """How long a stream trusts its user without reading them again: as long
    as the gate trusts who a subject is (auth.principal_cache_seconds)."""
    return get_config().auth.principal_cache_seconds


def stream_keys(scope: Scope, account_id: uuid.UUID | None) -> frozenset[str]:
    """
    The ring keys a stream of `scope` reads: all of Scope.ring_keys(), or,
    with an account filter, those with every mail and DAV account key but
    that one account's dropped -- an account that is not the caller's
    leaves none.
    """
    keys = scope.ring_keys()
    if account_id is None:
        return keys
    accounts = {str(a) for a in scope.account_ids} | {str(d) for d in scope.dav_account_ids}
    return (keys - accounts) | ({str(account_id)} & accounts)


async def _with_preamble(stream: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]:
    """
    Yield STREAM_PREAMBLE, then everything `stream` yields.

    Closes `stream` when this generator is closed, so its own cleanup (the
    ring waiter) runs on a client disconnect rather than whenever it is
    garbage-collected.
    """
    try:
        yield STREAM_PREAMBLE
        async for chunk in stream:
            yield chunk
    finally:
        await stream.aclose()


async def sse_endpoint(request: Request) -> StreamingResponse | JSONResponse:
    """
    SSE endpoint handler.

    Supports ?account_id=<uuid> to narrow the stream to one of the caller's
    accounts (stream_keys). Supports Last-Event-ID header (auto-reconnect)
    and ?last_event_id query parameter (manual reconnect) for replay.
    """
    if _event_ring is None:
        return StreamingResponse(
            iter([": server not ready\n\n"]),
            media_type="text/event-stream",
            status_code=503,
        )
    principal = current_principal()
    if principal is None:
        return JSONResponse({"detail": "not_authenticated"}, status_code=401)

    filter_account_id: uuid.UUID | None = None
    raw_account_id = request.query_params.get("account_id")
    if raw_account_id:
        try:
            filter_account_id = uuid.UUID(raw_account_id)
        except ValueError:
            pass

    async def resolve_keys() -> frozenset[str]:
        return stream_keys(await _scope_of(principal), filter_account_id)

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
        _with_preamble(
            _sse_generator(
                _event_ring, resolve_keys, last_event_id, request,
                recheck_seconds=_recheck_seconds(),
            )
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
