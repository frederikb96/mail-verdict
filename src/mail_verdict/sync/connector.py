"""
IMAP connection management via imap-tools.

Handles connect, authenticate, SSL/STARTTLS auto-detection,
connection pooling per account, and reconnection with exponential backoff.
All IMAP operations run in threads via asyncio.to_thread since imap-tools
is synchronous.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

from imap_tools import BaseMailBox, MailBox, MailBoxStartTls, MailBoxUnencrypted

from mail_verdict.core.retry import RetryConfig

logger = logging.getLogger(__name__)


@dataclass
class AccountConnConfig:
    """Lightweight account connection config for IMAP/SMTP."""

    name: str
    host: str
    port: int
    username: str
    password: str
    ssl_verify: bool = True
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    folders: list[str] = field(default_factory=lambda: ["INBOX"])
    idle_folders: list[str] = field(default_factory=lambda: ["INBOX"])


class IMAPConnectionError(Exception):
    """Raised when IMAP connection fails."""


class IMAPConnector:
    """
    Manages IMAP connections for a single account using imap-tools.

    Provides:
    - SSL/STARTTLS auto-detection based on port
    - Connection pooling (one MailBox per thread, not shared across await)
    - Dedicated persistent connections for IDLE
    - Exponential backoff reconnection
    """

    def __init__(
        self,
        account: AccountConnConfig,
        retry_config: RetryConfig,
    ) -> None:
        """
        Initialize connector for an account.

        Args:
            account: Account connection config with host/port/credentials
            retry_config: Retry configuration
        """
        self._account = account
        self._retry = retry_config
        self._pool: asyncio.Queue[BaseMailBox] = asyncio.Queue()
        self._pool_size = 0
        self._max_pool_size = 3
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def account_name(self) -> str:
        """Account identifier for logging."""
        return self._account.name

    async def connect(self) -> BaseMailBox:
        """
        Create a new authenticated imap-tools MailBox connection.

        Port 993 uses implicit SSL, port 143 uses STARTTLS,
        other ports use unencrypted connection.

        Returns:
            Authenticated BaseMailBox instance

        Raises:
            IMAPConnectionError: If connection or auth fails
        """
        port = self._account.port
        host = self._account.host

        def _sync_connect() -> BaseMailBox:
            """Synchronous connection logic for asyncio.to_thread."""
            use_ssl = port == 993

            try:
                mailbox: BaseMailBox
                if use_ssl:
                    ssl_ctx: ssl.SSLContext | None = None
                    if not self._account.ssl_verify:
                        ssl_ctx = ssl.create_default_context()
                        ssl_ctx.check_hostname = False
                        ssl_ctx.verify_mode = ssl.CERT_NONE
                    mailbox = MailBox(host, port, ssl_context=ssl_ctx)
                elif port == 143:
                    mailbox = MailBoxStartTls(host, port)
                else:
                    mailbox = MailBoxUnencrypted(host, port)

                mailbox.login(
                    self._account.username,
                    self._account.password,
                    initial_folder=None,
                )
                return mailbox

            except Exception as exc:
                raise IMAPConnectionError(
                    f"Connection to {host}:{port} failed: {exc}"
                ) from exc

        try:
            return await asyncio.to_thread(_sync_connect)
        except IMAPConnectionError:
            raise
        except Exception as exc:
            raise IMAPConnectionError(
                f"Connection to {host}:{port} failed: {exc}"
            ) from exc

    async def connect_with_retry(self) -> BaseMailBox:
        """
        Connect with exponential backoff retry.

        Returns:
            Authenticated BaseMailBox instance

        Raises:
            IMAPConnectionError: If all retries exhausted
        """
        last_error: Exception | None = None

        for attempt in range(self._retry.max_retries + 1):
            try:
                return await self.connect()
            except IMAPConnectionError as exc:
                last_error = exc
                if attempt < self._retry.max_retries:
                    delay = self._retry.delay_for_attempt(attempt)
                    logger.warning(
                        "Connection attempt failed, retrying",
                        extra={
                            "account": self.account_name,
                            "attempt": attempt + 1,
                            "delay_seconds": delay,
                            "error": str(exc),
                        },
                    )
                    await asyncio.sleep(delay)

        raise IMAPConnectionError(
            f"All {self._retry.max_retries + 1} connection attempts failed "
            f"for {self.account_name}: {last_error}"
        )

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[BaseMailBox]:
        """
        Acquire a connection from the pool.

        Returns the connection to the pool after use. Creates a new
        connection if the pool is empty and under max size.

        Yields:
            Authenticated BaseMailBox connection
        """
        conn: BaseMailBox | None = None

        try:
            if not self._pool.empty():
                conn = self._pool.get_nowait()
                if not await self._is_alive(conn):
                    await self._close_connection(conn)
                    conn = None

            if conn is None:
                async with self._lock:
                    conn = await self.connect_with_retry()
                    self._pool_size += 1

            yield conn

        except Exception:
            if conn is not None:
                await self._close_connection(conn)
                async with self._lock:
                    self._pool_size -= 1
                conn = None
            raise
        finally:
            if conn is not None:
                if not self._closed:
                    self._pool.put_nowait(conn)
                else:
                    await self._close_connection(conn)
                    async with self._lock:
                        self._pool_size -= 1

    async def create_idle_connection(self) -> BaseMailBox:
        """
        Create a dedicated connection for IDLE.

        IDLE connections are not pooled - the caller manages their lifecycle.

        Returns:
            Authenticated BaseMailBox for IDLE use
        """
        return await self.connect_with_retry()

    async def close(self) -> None:
        """Close all pooled connections and prevent new acquisitions."""
        self._closed = True

        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                await self._close_connection(conn)
            except asyncio.QueueEmpty:
                break

        self._pool_size = 0
        logger.info(
            "Connector closed",
            extra={"account": self.account_name},
        )

    async def _is_alive(self, conn: BaseMailBox) -> bool:
        """
        Check if a connection is still usable via NOOP.

        Args:
            conn: Connection to check
        """
        def _sync_noop() -> bool:
            try:
                conn.client.noop()
                return True
            except Exception:
                return False

        try:
            return await asyncio.to_thread(_sync_noop)
        except Exception:
            return False

    async def _close_connection(self, conn: BaseMailBox) -> None:
        """
        Safely close a single connection.

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
