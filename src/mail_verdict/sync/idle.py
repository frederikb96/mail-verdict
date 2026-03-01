"""
IMAP IDLE watcher for near-real-time mail detection.

Maintains a persistent IDLE connection per folder, auto-restarts
every 25 minutes (RFC 2177), reconnects with exponential backoff
on disconnect.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable, Coroutine

from mail_verdict.sync.connector import IMAPConnector
from mail_verdict.sync.extensions import AsyncIMAPExtended

if TYPE_CHECKING:
    from mail_verdict.config import RetryConfig, SyncConfig

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
        sync_config: SyncConfig,
        retry_config: RetryConfig,
        on_new_mail: Callable[[str], Coroutine[None, None, None]],
    ) -> None:
        """
        Initialize IDLE watcher.

        Args:
            connector: IMAP connector for creating dedicated connections
            sync_config: Sync configuration (idle_restart_seconds, idle_enabled)
            retry_config: Retry configuration for reconnection
            on_new_mail: Callback coroutine called with folder name on new mail
        """
        self._connector = connector
        self._sync_config = sync_config
        self._retry_config = retry_config
        self._on_new_mail = on_new_mail
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running = False

    async def start(self, folders: list[str]) -> None:
        """
        Start IDLE watchers for the given folders.

        Args:
            folders: Folder names to watch (e.g., ["INBOX"])
        """
        if not self._sync_config.idle_enabled:
            logger.info(
                "IDLE disabled in config",
                extra={"account": self._connector.account_name},
            )
            return

        if not self._connector.has_idle():
            logger.warning(
                "Server does not support IDLE",
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
            conn: AsyncIMAPExtended | None = None

            try:
                conn = await self._connector.create_idle_connection()
                consecutive_failures = 0

                # Select the folder
                result = await conn.select_plain(folder)
                if not result.ok:
                    logger.error(
                        "Failed to SELECT for IDLE",
                        extra={
                            "account": self._connector.account_name,
                            "folder": folder,
                        },
                    )
                    await self._close_idle_conn(conn)
                    await asyncio.sleep(self._retry_config.get_delay(0))
                    continue

                # IDLE loop with periodic restart
                while self._running:
                    idle_future = await conn.client.idle_start(
                        timeout=self._sync_config.idle_restart_seconds,
                    )

                    try:
                        push = await conn.client.wait_server_push(
                            timeout=self._sync_config.idle_restart_seconds,
                        )

                        # Check if we got an EXISTS notification
                        if push and push.lines:
                            for line in push.lines:
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

                    except asyncio.TimeoutError:
                        # Normal IDLE restart cycle
                        pass

                    # End IDLE before restarting
                    conn.client.idle_done()
                    await asyncio.sleep(0.1)

                    if idle_future.done():
                        idle_result = idle_future.result()
                        if hasattr(idle_result, "result") and idle_result.result != "OK":
                            logger.warning(
                                "IDLE ended with error, reconnecting",
                                extra={
                                    "account": self._connector.account_name,
                                    "folder": folder,
                                },
                            )
                            break

            except asyncio.CancelledError:
                if conn:
                    await self._close_idle_conn(conn)
                raise

            except Exception as exc:
                consecutive_failures += 1
                delay = self._retry_config.get_delay(
                    min(consecutive_failures - 1, self._retry_config.max_retries)
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

    async def _close_idle_conn(self, conn: AsyncIMAPExtended) -> None:
        """
        Safely close an IDLE connection.

        Args:
            conn: Connection to close
        """
        try:
            await conn.client.logout()
        except Exception:
            pass
