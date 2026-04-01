"""
PostgreSQL LISTEN/NOTIFY event dispatcher.

Listens for message changes on PostIMAP's tables via a custom trigger
(mv_message_notify) and dispatches events to:
- EventRing (SSE push to UI)
- Spam pipeline (new mail → verdict)
- Rules engine (new mail → evaluate rules)
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

MV_CHANNEL = "mv_messages"
ACCOUNT_CHANNEL = "account_changes"

TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION mv_message_notify() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    PERFORM pg_notify('mv_messages', json_build_object(
      'op', 'insert',
      'id', NEW.id::text,
      'account_id', NEW.account_id::text,
      'folder_id', NEW.folder_id::text
    )::text);
  ELSIF TG_OP = 'UPDATE' THEN
    PERFORM pg_notify('mv_messages', json_build_object(
      'op', 'update',
      'id', NEW.id::text,
      'account_id', NEW.account_id::text,
      'folder_id', NEW.folder_id::text,
      'old_folder_id', OLD.folder_id::text,
      'is_seen', NEW.is_seen,
      'is_flagged', NEW.is_flagged
    )::text);
  ELSIF TG_OP = 'DELETE' THEN
    PERFORM pg_notify('mv_messages', json_build_object(
      'op', 'delete',
      'id', OLD.id::text,
      'account_id', OLD.account_id::text,
      'folder_id', OLD.folder_id::text
    )::text);
  END IF;
  RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'mv_message_notify_trg'
    AND tgrelid = 'messages'::regclass
  ) THEN
    CREATE TRIGGER mv_message_notify_trg
    AFTER INSERT OR UPDATE OR DELETE ON messages
    FOR EACH ROW EXECUTE FUNCTION mv_message_notify();
  END IF;
END
$$;
"""

EventHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


@dataclass
class MessageEvent:
    """Parsed message change event from PG NOTIFY."""

    op: str
    message_id: str
    account_id: str
    folder_id: str
    old_folder_id: str | None = None
    is_seen: bool | None = None
    is_flagged: bool | None = None


class PgListener:
    """Listens on PostgreSQL NOTIFY channels and dispatches events."""

    def __init__(self, dsn: str) -> None:
        """Initialize listener with database connection string.

        Args:
            dsn: PostgreSQL connection string (e.g., postgresql://user:pass@host/db)
        """
        self._dsn = dsn
        self._conn: asyncpg.Connection | None = None
        self._task: asyncio.Task[None] | None = None
        self._handlers: list[EventHandler] = []
        self._running = False

    def add_handler(self, handler: EventHandler) -> None:
        """Register an event handler for message changes.

        Args:
            handler: Async callable receiving event dict
        """
        self._handlers.append(handler)

    async def start(self) -> None:
        """Start listening. Creates trigger if needed, begins LISTEN loop."""
        self._running = True
        self._conn = await asyncpg.connect(self._dsn)

        await self._conn.execute(TRIGGER_SQL)
        logger.info("PG trigger mv_message_notify ensured on messages table")

        await self._conn.add_listener(MV_CHANNEL, self._on_notify)
        await self._conn.add_listener(ACCOUNT_CHANNEL, self._on_notify)
        logger.info("PG LISTEN active on channels: %s, %s", MV_CHANNEL, ACCOUNT_CHANNEL)

        self._task = asyncio.create_task(self._keepalive(), name="pg-listener-keepalive")

    async def stop(self) -> None:
        """Stop listening and close connection."""
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
                await self._conn.remove_listener(MV_CHANNEL, self._on_notify)
                await self._conn.remove_listener(ACCOUNT_CHANNEL, self._on_notify)
            except Exception:
                pass
            await self._conn.close()
            self._conn = None
            logger.info("PG LISTEN stopped")

    def _on_notify(
        self,
        connection: asyncpg.Connection,
        pid: int,
        channel: str,
        payload: str,
    ) -> None:
        """Handle raw NOTIFY callback — parse and dispatch asynchronously."""
        try:
            data = json.loads(payload)
            data["_channel"] = channel
        except (json.JSONDecodeError, TypeError):
            logger.warning("Invalid NOTIFY payload on %s: %s", channel, payload)
            return

        for handler in self._handlers:
            asyncio.create_task(self._safe_dispatch(handler, data))

    async def _safe_dispatch(
        self, handler: EventHandler, data: dict[str, Any],
    ) -> None:
        """Dispatch event to handler with error isolation."""
        try:
            await handler(data)
        except Exception:
            logger.exception("Error in PG LISTEN handler")

    async def _keepalive(self) -> None:
        """Periodic keepalive to detect connection drops."""
        while self._running:
            try:
                await asyncio.sleep(30)
                if self._conn and not self._conn.is_closed():
                    await self._conn.execute("SELECT 1")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("PG LISTEN keepalive failed, reconnecting")
                await self._reconnect()

    async def _reconnect(self) -> None:
        """Reconnect after connection loss."""
        try:
            if self._conn:
                try:
                    await self._conn.close()
                except Exception:
                    pass

            self._conn = await asyncpg.connect(self._dsn)
            await self._conn.execute(TRIGGER_SQL)
            await self._conn.add_listener(MV_CHANNEL, self._on_notify)
            await self._conn.add_listener(ACCOUNT_CHANNEL, self._on_notify)
            logger.info("PG LISTEN reconnected")
        except Exception:
            logger.exception("PG LISTEN reconnection failed, retrying in 5s")
            await asyncio.sleep(5)


def parse_dsn_from_sqlalchemy_url(url: str) -> str:
    """Convert SQLAlchemy async URL to asyncpg DSN.

    Args:
        url: SQLAlchemy URL (e.g., postgresql+asyncpg://user:pass@host/db)

    Returns:
        asyncpg DSN (e.g., postgresql://user:pass@host/db)
    """
    return url.replace("postgresql+asyncpg://", "postgresql://")
