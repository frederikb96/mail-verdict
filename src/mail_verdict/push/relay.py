"""
Native push through a push relay.

A native app registers its device with a relay and hands this server the
ticket the relay issued plus a content key of its own. This module posts
each notification to that relay, sealed with the content key
(push/envelope.py), so the relay forwards ciphertext it cannot read. The
relay's HTTP API is specified in mail-verdict-ios/relay/README.md:
https://github.com/frederikb96/mail-verdict-ios/blob/main/relay/README.md

The ticket and content key are stored encrypted under ENCRYPTION_KEY and
decrypted per send. A relay missing from push.apns_relay_urls is never
sent to, so emptying that list stops all traffic to relays.

Read-sync: when alerts are dismissed or resolved, every native device is
sent one silent push so it can clear banners for mail read elsewhere --
at most once per push.read_sync_min_interval_seconds, with a change inside
that window sent once at its end.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any

import httpx

from mail_verdict.core.encryption import EncryptionError, decrypt, encrypt
from mail_verdict.database.repository import PushSubscriptionRepository

if TYPE_CHECKING:
    from mail_verdict.config.loader import PushConfig
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.database.models import PushSubscription

logger = logging.getLogger(__name__)

_PUSH_PATH = "/v1/push"
_READ_SYNC_COLLAPSE_ID = "mv-read-sync"
# A read-sync wake is only useful soon; one Apple delivers hours later
# reconciles nothing the next foreground would not.
_READ_SYNC_TTL_SECONDS = 600


class RelayOutcome(enum.Enum):
    """What one push to one device came to."""

    DELIVERED = "delivered"
    # The relay says this device can never be reached with this ticket
    # again (401 ticket invalid or expired, 410 token gone): the row goes.
    GONE = "gone"
    # Anything else -- rate limited, relay or Apple down, a timeout.
    FAILED = "failed"


class RelayClient:
    """This server's side of the push relay protocol."""

    def __init__(
        self,
        *,
        encryption_key: str,
        config: PushConfig,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """
        Args:
            encryption_key: ENCRYPTION_KEY -- protects stored tickets and keys
            config: The push section of the infrastructure config
            transport: An httpx transport, for tests
            clock, sleep: The read-sync throttle's time source, for tests
        """
        self._encryption_key = encryption_key
        self._relay_urls = list(config.apns_relay_urls)
        self._timeout = config.relay_timeout_seconds
        self._read_sync_interval = config.read_sync_min_interval_seconds
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._last_read_sync: float | None = None
        self._trailing_read_sync: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def relay_urls(self) -> list[str]:
        """The relays a device may register against."""
        return list(self._relay_urls)

    def unavailable_reason(self) -> str | None:
        """Why native push cannot work on this server, or None if it can."""
        if not self._encryption_key:
            return "ENCRYPTION_KEY is not configured on this server"
        if not self._relay_urls:
            return "This server has native push disabled (push.apns_relay_urls is empty)"
        return None

    def allows(self, relay_url: str | None) -> bool:
        """Whether this relay is one a device may use and this server sends to."""
        return self.unavailable_reason() is None and relay_url in self._relay_urls

    def seal_credentials(self, ticket: str, content_key: bytes) -> tuple[bytes, bytes]:
        """Encrypt a device's relay ticket and content key for storage."""
        return (
            encrypt(ticket, self._encryption_key),
            encrypt(content_key.hex(), self._encryption_key),
        )

    def open_credentials(self, sub: PushSubscription) -> tuple[str, bytes]:
        """
        Decrypt a native row's ticket and content key.

        Raises:
            EncryptionError: They were stored under a different ENCRYPTION_KEY
        """
        if sub.encrypted_relay_ticket is None or sub.encrypted_content_key is None:
            raise EncryptionError("not a native subscription")
        ticket = decrypt(sub.encrypted_relay_ticket, self._encryption_key)
        key = bytes.fromhex(decrypt(sub.encrypted_content_key, self._encryption_key))
        return ticket, key

    async def send_alert(
        self, sub: PushSubscription, *, ticket: str, blob: str, collapse_id: str,
        ttl_seconds: int,
    ) -> RelayOutcome:
        """Post one sealed alert to the device's relay."""
        return await self._post(sub, {
            "ticket": ticket, "type": "alert", "blob": blob,
            "collapse_id": collapse_id, "ttl_seconds": ttl_seconds,
        })

    async def send_background(self, sub: PushSubscription) -> RelayOutcome:
        """Post one silent wake to the device's relay."""
        try:
            ticket, _key = self.open_credentials(sub)
        except EncryptionError:
            return RelayOutcome.GONE
        return await self._post(sub, {
            "ticket": ticket, "type": "background",
            "collapse_id": _READ_SYNC_COLLAPSE_ID, "ttl_seconds": _READ_SYNC_TTL_SECONDS,
        })

    async def _post(self, sub: PushSubscription, body: dict[str, Any]) -> RelayOutcome:
        assert sub.relay_url is not None
        url = sub.relay_url.rstrip("/") + _PUSH_PATH
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport,
            ) as client:
                response = await client.post(url, json=body)
        except httpx.HTTPError as exc:
            logger.warning(
                "Push relay unreachable", extra={"subscription_id": str(sub.id), "error": str(exc)},
            )
            return RelayOutcome.FAILED
        return _outcome_for(response, sub)

    def schedule_read_sync(self, db: DatabaseConnection) -> None:
        """
        Arrange one silent push to every native device -- now, if the last
        one was at least the minimum interval ago, otherwise once at the
        end of that interval. Calls inside a pending window fold into it.
        """
        if self.unavailable_reason() is not None:
            return
        if self._trailing_read_sync is not None and not self._trailing_read_sync.done():
            return
        now = self._clock()
        wait = (
            0.0 if self._last_read_sync is None
            else self._last_read_sync + self._read_sync_interval - now
        )
        if wait <= 0:
            self._last_read_sync = now
            self._spawn(self._read_sync(db))
        else:
            self._last_read_sync = now + wait
            self._trailing_read_sync = self._spawn(self._read_sync_after(db, wait))

    async def _read_sync_after(self, db: DatabaseConnection, wait: float) -> None:
        await self._sleep(wait)
        await self._read_sync(db)

    async def _read_sync(self, db: DatabaseConnection) -> None:
        """One silent push to every native device whose relay is allowed."""
        try:
            repo = PushSubscriptionRepository(db)
            rows = [s for s in await repo.list_native() if self.allows(s.relay_url)]
            outcomes = await asyncio.gather(*(self.send_background(s) for s in rows))
            for sub, outcome in zip(rows, outcomes):
                if outcome is RelayOutcome.GONE:
                    await repo.delete(sub.id)
                elif outcome is RelayOutcome.DELIVERED:
                    await repo.mark_seen(sub.id)
        except Exception:
            logger.exception("Read-sync push failed")

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        # asyncio only weakly references a task nothing else holds.
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task


def _outcome_for(response: httpx.Response, sub: PushSubscription) -> RelayOutcome:
    """Map the relay's answer to what happens to the subscription row."""
    status = response.status_code
    if 200 <= status < 300:
        return RelayOutcome.DELIVERED
    if status in (401, 410):
        logger.info(
            "Push relay reports the device unreachable for good",
            extra={"subscription_id": str(sub.id), "status": status},
        )
        return RelayOutcome.GONE
    if status == 429 or status >= 500:
        logger.info(
            "Push relay declined for now", extra={"subscription_id": str(sub.id), "status": status},
        )
    else:
        # 400/413: the request this server built was refused as malformed.
        logger.warning(
            "Push relay rejected the request",
            extra={"subscription_id": str(sub.id), "status": status, "body": response.text[:200]},
        )
    return RelayOutcome.FAILED


_relay_client: RelayClient | None = None


def init_relay_client(encryption_key: str, config: PushConfig) -> RelayClient:
    """Initialize the process-wide relay client."""
    global _relay_client
    _relay_client = RelayClient(encryption_key=encryption_key, config=config)
    return _relay_client


def get_relay_client() -> RelayClient:
    """
    Get the process-wide relay client.

    Raises:
        RuntimeError: If not initialized
    """
    if _relay_client is None:
        raise RuntimeError("RelayClient not initialized")
    return _relay_client


def get_relay_client_if_ready() -> RelayClient | None:
    """The relay client, or None outside a running server (a test, a script)."""
    return _relay_client


def reset_relay_client() -> None:
    """Reset the process-wide relay client. Useful for testing."""
    global _relay_client
    _relay_client = None
