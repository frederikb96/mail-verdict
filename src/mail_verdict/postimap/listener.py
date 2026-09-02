"""
PostgreSQL LISTEN/NOTIFY dispatcher for the postimap_events channel.

The single channel covers messages, folders, accounts and outbox. Payloads
are typed and dispatched to registered handlers; MailVerdict never installs
DDL on a PostIMAP-owned table -- this channel is the entire event surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import asyncpg  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

CHANNEL = "postimap_events"

KEEPALIVE_INTERVAL_S = 30
RECONNECT_DELAY_S = 5
RECONNECT_MAX_DELAY_S = 60

# A bounded pool of dispatch workers draining a queue, rather than one
# asyncio task per event: during a sync burst a NOTIFY channel can fire
# thousands of events, and each handler call typically does a database
# write, so unbounded concurrency there would starve the HTTP handlers
# sharing the same connection pool (roughly fifteen connections). Bounding
# it also means every event in flight is held by the queue itself until a
# worker picks it up, rather than by a bare task object nothing else
# references -- an unreferenced asyncio.Task is eligible for garbage
# collection mid-flight, which would drop the event it was dispatching.
DISPATCH_CONCURRENCY = 4
DISPATCH_QUEUE_MAXSIZE = 10_000


@dataclass(frozen=True)
class PostimapEvent:
    """A single parsed postimap_events NOTIFY payload.

    Four more `type` values cover calendars and contacts: `dav_account`,
    `dav_collection`, `dav_object`, `dav_notification`. `account_id`
    carries `dav_accounts.id` for all four, exactly as it carries
    `accounts.id` for mail -- a handler filtering by mail account id never
    matches one of these. `collection_id` is present on the latter three;
    `old_collection_id` only on a `dav_object` move, mirroring
    `old_folder_id`.
    """

    v: int
    type: str
    op: str
    id: str
    account_id: str
    folder_id: str | None = None
    origin: str | None = None
    changed: tuple[str, ...] = ()
    backfill: bool = False
    # Present only when changed includes "folder_id" (a move made inside
    # this application); omitted entirely otherwise, never null -- read
    # with .get(), not raw["old_folder_id"].
    old_folder_id: str | None = None
    # DAV events only -- see the class docstring.
    collection_id: str | None = None
    old_collection_id: str | None = None
    # Present only on type="dav_notification": put|move|delete|mkcol|proppatch|rmcol.
    action: str | None = None

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> PostimapEvent:
        """
        Parse a raw NOTIFY payload dict into a typed PostimapEvent.

        Args:
            raw: Decoded JSON payload from postimap_events

        Raises:
            KeyError: If a required field is missing
            TypeError: If a field has the wrong type
        """
        return cls(
            v=int(raw["v"]),
            type=str(raw["type"]),
            op=str(raw["op"]),
            id=str(raw["id"]),
            account_id=str(raw["account_id"]),
            folder_id=str(raw["folder_id"]) if raw.get("folder_id") else None,
            origin=str(raw["origin"]) if raw.get("origin") else None,
            changed=tuple(raw.get("changed") or ()),
            backfill=bool(raw.get("backfill", False)),
            old_folder_id=str(raw["old_folder_id"]) if raw.get("old_folder_id") else None,
            collection_id=str(raw["collection_id"]) if raw.get("collection_id") else None,
            old_collection_id=(
                str(raw["old_collection_id"]) if raw.get("old_collection_id") else None
            ),
            action=str(raw["action"]) if raw.get("action") else None,
        )


EventHandler = Callable[[PostimapEvent], Coroutine[Any, Any, None]]
ReconnectHandler = Callable[[], Coroutine[Any, Any, None]]


class PostimapListener:
    """Listens on postimap_events and dispatches parsed events to handlers."""

    def __init__(self, dsn: str) -> None:
        """
        Initialize the listener with a database connection string.

        Args:
            dsn: asyncpg-compatible DSN (postgresql://user:pass@host/db)
        """
        self._dsn = dsn
        self._conn: asyncpg.Connection | None = None
        self._task: asyncio.Task[None] | None = None
        self._handlers: list[EventHandler] = []
        self._reconnect_handlers: list[ReconnectHandler] = []
        self._running = False
        self._queue: asyncio.Queue[PostimapEvent] = asyncio.Queue(maxsize=DISPATCH_QUEUE_MAXSIZE)
        self._dispatch_tasks: list[asyncio.Task[None]] = []

    def add_handler(self, handler: EventHandler) -> None:
        """
        Register an event handler.

        Args:
            handler: Async callable receiving a parsed PostimapEvent
        """
        self._handlers.append(handler)

    def add_reconnect_handler(self, handler: ReconnectHandler) -> None:
        """
        Register a callback run once a reconnect succeeds.

        A reconnect loses any NOTIFY fired during the gap (see
        _keepalive's docstring) -- this is where a caller broadcasts the
        "invalidate everything" signal every currently connected client
        needs, not only one whose own SSE connection happens to drop and
        later replay a stale Last-Event-ID.

        Args:
            handler: Async callable taking no arguments, run once per
                successful reconnect
        """
        self._reconnect_handlers.append(handler)

    async def start(self) -> None:
        """Start listening on postimap_events."""
        self._running = True
        self._conn = await asyncpg.connect(self._dsn)
        await self._conn.add_listener(CHANNEL, self._on_notify)
        logger.info("PostIMAP event listener active", extra={"channel": CHANNEL})

        self._task = asyncio.create_task(self._keepalive(), name="postimap-listener-keepalive")
        self._dispatch_tasks = [
            asyncio.create_task(self._dispatch_loop(), name=f"postimap-listener-dispatch-{i}")
            for i in range(DISPATCH_CONCURRENCY)
        ]

    async def stop(self) -> None:
        """Stop listening and close the connection."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        for dispatch_task in self._dispatch_tasks:
            dispatch_task.cancel()
        for dispatch_task in self._dispatch_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await dispatch_task
        self._dispatch_tasks = []

        if self._conn:
            try:
                await self._conn.remove_listener(CHANNEL, self._on_notify)
            except Exception:
                logger.debug("Failed to remove listener during shutdown", exc_info=True)
            await self._conn.close()
            self._conn = None
            logger.info("PostIMAP event listener stopped")

    def _on_notify(
        self,
        connection: asyncpg.Connection,
        pid: int,
        channel: str,
        payload: str,
    ) -> None:
        """Handle a raw NOTIFY callback -- parse and queue for dispatch.

        A plain (non-async) callback, called synchronously by asyncpg, so
        this can only enqueue -- it cannot await a dispatch worker picking
        the event up. `put_nowait` is what keeps that non-blocking; the
        queue's bound only bites under sustained overload far beyond any
        realistic burst (see DISPATCH_QUEUE_MAXSIZE), and even then it logs
        the drop rather than losing the event without a trace.
        """
        try:
            raw = json.loads(payload)
            event = PostimapEvent.from_payload(raw)
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            logger.warning("Invalid postimap_events payload: %s", payload, exc_info=True)
            return

        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "postimap_events dispatch queue full, dropping event",
                extra={"event_type": event.type, "event_id": event.id},
            )

    async def _dispatch_loop(self) -> None:
        """One of DISPATCH_CONCURRENCY consumers draining the queue, each
        handler run in turn for a given event -- bounding how many handler
        calls (each typically a database write) are in flight at once,
        however many events a burst enqueues."""
        while True:
            event = await self._queue.get()
            try:
                for handler in self._handlers:
                    await self._safe_dispatch(handler, event)
            finally:
                self._queue.task_done()

    async def _safe_dispatch(self, handler: EventHandler, event: PostimapEvent) -> None:
        """Dispatch an event to a handler with error isolation."""
        try:
            await handler(event)
        except Exception:
            logger.exception("Error in postimap_events handler", extra={"event_type": event.type})

    async def _keepalive(self) -> None:
        """Periodic keepalive; reconnects on failure.

        A closed connection triggers a reconnect the same as a failed probe
        -- asyncpg marks a connection closed without raising (a Postgres
        restart, a failover, an idle reaper, a network blip), so treating
        `is_closed()` as a reason to skip the probe leaves the loop spinning
        on a dead connection forever instead of ever calling _reconnect().

        A reconnect loses any NOTIFY fired during the gap -- callers should
        treat reconnection as a signal to invalidate cached state broadly
        rather than assume no events were missed.
        """
        while self._running:
            try:
                await asyncio.sleep(KEEPALIVE_INTERVAL_S)
                if self._conn is None or self._conn.is_closed():
                    raise ConnectionError("postimap_events connection is closed")
                await self._conn.execute("SELECT 1")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("postimap_events keepalive failed, reconnecting", exc_info=True)
                await self._reconnect()

    async def _reconnect(self) -> None:
        """Reconnect after a connection loss, retrying with backoff until it succeeds.

        A single failed attempt used to leave `self._conn` pointing at the
        already-closed connection, so the next keepalive tick would skip its
        probe again -- retrying here instead of returning after one attempt
        is what actually recovers the listener.
        """
        if self._conn:
            try:
                await self._conn.close()
            except Exception:
                logger.debug("Failed to close stale connection before reconnect", exc_info=True)
            self._conn = None

        delay = RECONNECT_DELAY_S
        while self._running:
            try:
                self._conn = await asyncpg.connect(self._dsn)
                await self._conn.add_listener(CHANNEL, self._on_notify)
                logger.info("postimap_events listener reconnected")
                await self._notify_reconnect_handlers()
                return
            except Exception:
                logger.exception("postimap_events reconnect failed, retrying in %ss", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_DELAY_S)

    async def _notify_reconnect_handlers(self) -> None:
        """Run every registered reconnect handler, isolating one's failure
        from the rest -- the same error-isolation _safe_dispatch gives
        ordinary event handlers."""
        for handler in self._reconnect_handlers:
            try:
                await handler()
            except Exception:
                logger.exception("Error in postimap_events reconnect handler")


def parse_dsn_from_sqlalchemy_url(url: str) -> str:
    """
    Convert a SQLAlchemy async URL to an asyncpg DSN.

    Args:
        url: SQLAlchemy URL (e.g. postgresql+asyncpg://user:pass@host/db)

    Returns:
        asyncpg DSN (e.g. postgresql://user:pass@host/db)
    """
    return url.replace("postgresql+asyncpg://", "postgresql://")
