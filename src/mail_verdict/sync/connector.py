"""
IMAP connection management.

Handles connect, authenticate, SSL/TLS, connection pooling per account,
capability detection, and reconnection with exponential backoff.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

from aioimaplib import IMAP4, IMAP4_SSL

from mail_verdict.sync.extensions import AsyncIMAPExtended

if TYPE_CHECKING:
    from mail_verdict.config import AccountConfig, RetryConfig

logger = logging.getLogger(__name__)


class IMAPConnectionError(Exception):
    """Raised when IMAP connection fails."""


class IMAPConnector:
    """
    Manages IMAP connections for a single account.

    Provides:
    - SSL/TLS connection with STARTTLS fallback
    - Connection pooling for command connections
    - Dedicated persistent connections for IDLE
    - Capability detection (CONDSTORE, QRESYNC, SPECIAL-USE, IDLE)
    - Exponential backoff reconnection
    """

    def __init__(
        self,
        account: AccountConfig,
        retry: RetryConfig,
    ) -> None:
        """
        Initialize connector for an account.

        Args:
            account: Account configuration with host/port/credentials
            retry: Retry configuration for reconnection backoff
        """
        self._account = account
        self._retry = retry
        self._pool: asyncio.Queue[AsyncIMAPExtended] = asyncio.Queue()
        self._pool_size = 0
        self._max_pool_size = 3
        self._capabilities: set[str] = set()
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def account_name(self) -> str:
        """Account identifier for logging."""
        return self._account.name

    @property
    def capabilities(self) -> set[str]:
        """Server capabilities detected on first connection."""
        return self._capabilities

    def has_condstore(self) -> bool:
        """Check CONDSTORE support."""
        return "CONDSTORE" in self._capabilities

    def has_qresync(self) -> bool:
        """Check QRESYNC support."""
        return "QRESYNC" in self._capabilities

    def has_special_use(self) -> bool:
        """Check SPECIAL-USE support."""
        return "SPECIAL-USE" in self._capabilities

    def has_idle(self) -> bool:
        """Check IDLE support."""
        return "IDLE" in self._capabilities

    async def connect(self) -> AsyncIMAPExtended:
        """
        Create a new authenticated IMAP connection.

        Tries SSL first (port 993), falls back to STARTTLS (port 143).

        Returns:
            Authenticated AsyncIMAPExtended instance

        Raises:
            IMAPConnectionError: If connection or auth fails
        """
        port = self._account.port
        host = self._account.host
        use_ssl = port == 993

        try:
            if use_ssl:
                ssl_ctx: ssl.SSLContext | None = None
                if not self._account.ssl_verify:
                    ssl_ctx = ssl.create_default_context()
                    ssl_ctx.check_hostname = False
                    ssl_ctx.verify_mode = ssl.CERT_NONE
                client = IMAP4_SSL(host=host, port=port, ssl_context=ssl_ctx)
            else:
                client = IMAP4(host=host, port=port)

            await client.wait_hello_from_server()

            if not use_ssl and client.has_capability("STARTTLS"):
                logger.info(
                    "STARTTLS available but aioimaplib lacks native support, "
                    "recommend using port 993 (SSL) instead",
                    extra={"account": self.account_name},
                )

            response = await client.login(
                self._account.username,
                self._account.password,
            )
            if response.result != "OK":
                raise IMAPConnectionError(
                    f"Login failed for {self.account_name}: {response.result}"
                )

            extended = AsyncIMAPExtended(client)

            if not self._capabilities:
                self._capabilities = extended.capabilities.copy()
                logger.info(
                    "Capabilities detected",
                    extra={
                        "account": self.account_name,
                        "condstore": self.has_condstore(),
                        "qresync": self.has_qresync(),
                        "special_use": self.has_special_use(),
                        "idle": self.has_idle(),
                    },
                )

            if extended.has_capability("QRESYNC"):
                await extended.enable_qresync()

            return extended

        except IMAPConnectionError:
            raise
        except Exception as exc:
            raise IMAPConnectionError(f"Connection to {host}:{port} failed: {exc}") from exc

    async def connect_with_retry(self) -> AsyncIMAPExtended:
        """
        Connect with exponential backoff retry.

        Returns:
            Authenticated AsyncIMAPExtended instance

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
                    delay = self._retry.get_delay(attempt)
                    print(f"IMAP connect failed: {exc}", flush=True)
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
    async def acquire(self) -> AsyncIterator[AsyncIMAPExtended]:
        """
        Acquire a connection from the pool.

        Returns the connection to the pool after use. Creates a new
        connection if the pool is empty and under max size.

        Yields:
            AsyncIMAPExtended connection
        """
        conn: AsyncIMAPExtended | None = None

        try:
            if not self._pool.empty():
                conn = self._pool.get_nowait()
                if not self._is_alive(conn):
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

    async def create_idle_connection(self) -> AsyncIMAPExtended:
        """
        Create a dedicated connection for IDLE.

        IDLE connections are not pooled - the caller manages their lifecycle.

        Returns:
            Authenticated AsyncIMAPExtended for IDLE use
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

    def _is_alive(self, conn: AsyncIMAPExtended) -> bool:
        """
        Check if a connection is still usable.

        Args:
            conn: Connection to check
        """
        try:
            state = conn.client.get_state()
            return state in ("AUTH", "SELECTED")
        except Exception:
            return False

    async def _close_connection(self, conn: AsyncIMAPExtended) -> None:
        """
        Safely close a single connection.

        Args:
            conn: Connection to close
        """
        try:
            await conn.client.logout()
        except Exception:
            pass
