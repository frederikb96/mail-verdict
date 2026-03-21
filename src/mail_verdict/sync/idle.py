"""
IMAP IDLE watcher for near-real-time mail detection.

Maintains a persistent IDLE connection per folder, auto-restarts
every 25 minutes (RFC 2177), reconnects with exponential backoff
on disconnect.

Uses imap-tools mailbox.idle.wait() in asyncio.to_thread since
imap-tools is synchronous.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

from imap_tools import BaseMailBox

from mail_verdict.core.retry import RetryConfig
from mail_verdict.sync.connector import IMAPConnector

logger = logging.getLogger(__name__)


class IdleWatcher:
    """
    Maintains IDLE connections for near-real-time notifications.

    One asyncio task per IDLE folder per account. Auto-restarts
    IDLE every idle_restart_seconds (default 25 min) to comply
    with RFC 2177 and prevent server-side timeouts.
    """

    def __init__(
        self,
        connector: IMAPConnector,
        sync_settings: dict[str, Any],
        retry_config: RetryConfig,
        on_new_mail: Callable[[str], Coroutine[None, None, None]],
    ) -> None:
        """
        Initialize IDLE watcher.

        Args:
            connector: IMAP connector for creating dedicated connections
            sync_settings: Sync settings dict (idle_restart_seconds, idle_enabled)
            retry_config: Retry configuration for reconnection
            on_new_mail: Callback coroutine called with folder name on new mail
        """
        self._connector = connector
        self._sync_settings = sync_settings
        self._retry = retry_config
        self._on_new_mail = on_new_mail
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running = False

    async def start(self, folders: list[str]) -> None:
        """
        Start IDLE watchers for the given folders.

        Args:
            folders: Folder names to watch (e.g., ["INBOX"])
        """
        if not self._sync_settings.get("idle_enabled", True):
            logger.info(
                "IDLE disabled in config",
                extra={"account": self._connector.account_name},
            )
            return

        self._running = True

        for folder in folders:
            if folder in self._tasks:
                continue

            task = asyncio.create_task(
                self._idle_loop(folder),
                name=f"idle-{self._connector.account_name}-{folder}",
            )
            self._tasks[folder] = task

        logger.info(
            "IDLE watchers started",
            extra={
                "account": self._connector.account_name,
                "folders": folders,
            },
        )

    async def stop(self) -> None:
        """Stop all IDLE watchers."""
        self._running = False

        for folder, task in self._tasks.items():
            if not task.done():
                task.cancel()

        for task in self._tasks.values():
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._tasks.clear()
        logger.info(
            "IDLE watchers stopped",
            extra={"account": self._connector.account_name},
        )

    async def _idle_loop(self, folder: str) -> None:
        """
        IDLE loop for a single folder with auto-restart and reconnection.

        Args:
            folder: IMAP folder name to watch
        """
        consecutive_failures = 0

        while self._running:
            conn: BaseMailBox | None = None

            try:
                conn = await self._connector.create_idle_connection()
                consecutive_failures = 0

                # Select the folder (conn is guaranteed non-None here)
                active_conn = conn

                def _sync_select() -> None:
                    active_conn.folder.set(folder)

                await asyncio.to_thread(_sync_select)

                # IDLE loop with periodic restart
                while self._running:
                    # imap-tools idle.wait() handles start/poll/stop internally
                    idle_timeout = int(
                        self._sync_settings.get("idle_restart_seconds", 1500)
                    )
                    # RFC 2177 mandates max 29 minutes
                    idle_timeout = min(idle_timeout, 29 * 60 - 60)
                    wait_timeout = idle_timeout

                    def _sync_idle() -> list[bytes]:
                        return active_conn.idle.wait(timeout=wait_timeout)

                    responses = await asyncio.to_thread(_sync_idle)

                    # Check if we got an EXISTS notification
                    if responses:
                        for line in responses:
                            text = (
                                line.decode(errors="replace")
                                if isinstance(line, bytes)
                                else str(line)
                            )
                            if "EXISTS" in text.upper():
                                logger.info(
                                    "IDLE: new mail detected",
                                    extra={
                                        "account": self._connector.account_name,
                                        "folder": folder,
                                    },
                                )
                                await self._on_new_mail(folder)
                                break

            except asyncio.CancelledError:
                if conn:
                    await self._close_idle_conn(conn)
                raise

            except Exception as exc:
                consecutive_failures += 1
                delay = self._retry.delay_for_attempt(
                    min(consecutive_failures - 1, self._retry.max_retries),
                )
                logger.warning(
                    "IDLE connection failed, reconnecting",
                    extra={
                        "account": self._connector.account_name,
                        "folder": folder,
                        "error": str(exc),
                        "delay_seconds": delay,
                    },
                )
                if self._running:
                    await asyncio.sleep(delay)

            finally:
                if conn:
                    await self._close_idle_conn(conn)

    async def _close_idle_conn(self, conn: BaseMailBox) -> None:
        """
        Safely close an IDLE connection.

        Args:
            conn: Connection to close
        """
        def _sync_logout() -> None:
            try:
                conn.logout()
            except Exception:
                pass

        try:
            await asyncio.to_thread(_sync_logout)
        except Exception:
            pass
