"""
PostgreSQL LISTEN/NOTIFY dispatcher for the postimap_events channel.

The single channel covers messages, folders, accounts and outbox. Payloads
are typed and dispatched to registered handlers; MailVerdict never installs
DDL on a PostIMAP-owned table -- this channel is the entire event surface.
"""

from __future__ import annotations

import asyncio
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


@dataclass(frozen=True)
class PostimapEvent:
    """A single parsed postimap_events NOTIFY payload."""

    v: int
    type: str
    op: str
    id: str
    account_id: str
    folder_id: str | None = None
    origin: str | None = None
    changed: tuple[str, ...] = ()
    backfill: bool = False

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
        )


EventHandler = Callable[[PostimapEvent], Coroutine[Any, Any, None]]


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
        self._running = False

    def add_handler(self, handler: EventHandler) -> None:
        """
        Register an event handler.

        Args:
            handler: Async callable receiving a parsed PostimapEvent
        """
        self._handlers.append(handler)

    async def start(self) -> None:
        """Start listening on postimap_events."""
        self._running = True
        self._conn = await asyncpg.connect(self._dsn)
        await self._conn.add_listener(CHANNEL, self._on_notify)
        logger.info("PostIMAP event listener active", extra={"channel": CHANNEL})

        self._task = asyncio.create_task(self._keepalive(), name="postimap-listener-keepalive")

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
        """Handle a raw NOTIFY callback -- parse and dispatch asynchronously."""
        try:
            raw = json.loads(payload)
            event = PostimapEvent.from_payload(raw)
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            logger.warning("Invalid postimap_events payload: %s", payload, exc_info=True)
            return

        for handler in self._handlers:
            asyncio.create_task(self._safe_dispatch(handler, event))

    async def _safe_dispatch(self, handler: EventHandler, event: PostimapEvent) -> None:
        """Dispatch an event to a handler with error isolation."""
        try:
            await handler(event)
        except Exception:
            logger.exception("Error in postimap_events handler", extra={"event_type": event.type})

    async def _keepalive(self) -> None:
        """Periodic keepalive; reconnects on failure.

        A reconnect loses any NOTIFY fired during the gap -- callers should
        treat reconnection as a signal to invalidate cached state broadly
        rather than assume no events were missed.
        """
        while self._running:
            try:
                await asyncio.sleep(KEEPALIVE_INTERVAL_S)
                if self._conn and not self._conn.is_closed():
                    await self._conn.execute("SELECT 1")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("postimap_events keepalive failed, reconnecting", exc_info=True)
                await self._reconnect()

    async def _reconnect(self) -> None:
        """Reconnect after a connection loss."""
        if self._conn:
            try:
                await self._conn.close()
            except Exception:
                logger.debug("Failed to close stale connection before reconnect", exc_info=True)

        try:
            self._conn = await asyncpg.connect(self._dsn)
            await self._conn.add_listener(CHANNEL, self._on_notify)
            logger.info("postimap_events listener reconnected")
        except Exception:
            logger.exception("postimap_events reconnect failed, retrying")
            await asyncio.sleep(RECONNECT_DELAY_S)


def parse_dsn_from_sqlalchemy_url(url: str) -> str:
    """
    Convert a SQLAlchemy async URL to an asyncpg DSN.

    Args:
        url: SQLAlchemy URL (e.g. postgresql+asyncpg://user:pass@host/db)

    Returns:
        asyncpg DSN (e.g. postgresql://user:pass@host/db)
    """
    return url.replace("postgresql+asyncpg://", "postgresql://")
