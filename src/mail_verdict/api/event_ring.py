"""
In-memory ring buffer for SSE events.

Every event goes on one key: a mail account, a DAV account, a user, the
admin key or the key every stream receives (auth/scope.py, Scope.ring_keys).
A stream reads exactly the keys its caller's scope covers, so replay from a
given Last-Event-ID and the live wake-ups both stay within them.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from collections import deque
from collections.abc import Collection
from datetime import datetime, timezone
from typing import Any

from mail_verdict.api.events import SSE_EVENT_TYPES

logger = logging.getLogger(__name__)

DEFAULT_RING_SIZE = 500


def _key_set(keys: Collection[str]) -> Collection[str]:
    """`keys`, refusing a lone string: iterated, it would read as a set of
    one-character keys that match nothing, and a stream would go silent."""
    if isinstance(keys, str):
        raise TypeError("ring keys are a collection of keys, not one string")
    return keys


class EventRing:
    """
    Per-key ring buffers of SSE events with one global sequence counter.

    Thread-safe via asyncio.Lock for concurrent add/replay operations.
    Each event gets a monotonically increasing ID used by SSE Last-Event-ID.
    """

    def __init__(self, max_size: int = DEFAULT_RING_SIZE) -> None:
        """
        Initialize the event ring.

        Args:
            max_size: Maximum events to retain per key
        """
        self._max_size = max_size
        self._lock = asyncio.Lock()
        self._seq: int = 0
        self._rings: dict[str, deque[dict[str, Any]]] = {}
        # The id of the newest event each key's ring has dropped: a replay
        # from any id below it would be missing that event.
        self._evicted: dict[str, int] = {}
        self._waiters: dict[str, list[asyncio.Event]] = {}
        # Sequence ids restart at 1 with every process, so an id alone
        # cannot say which ring issued it: a browser reconnecting after a
        # restart would present an old, higher id and be replayed nothing,
        # and then skip every new event until the counter caught up.
        # Prefixing every id handed out with this ring's own epoch lets a
        # reconnect tell "mine, replay from here" from "not mine, resync".
        self.epoch = secrets.token_hex(4)

    def format_event_id(self, seq: int) -> str:
        """The SSE `id:` value for a sequence id of this ring."""
        return f"{self.epoch}-{seq}"

    def parse_event_id(self, raw: str) -> int | None:
        """
        The sequence id a client's Last-Event-ID names, if this ring issued it.

        Args:
            raw: The value the client sent back

        Returns:
            The sequence id, or None when the value came from another ring
            (a previous process) or is not an id at all
        """
        epoch, sep, seq = raw.partition("-")
        if not sep or epoch != self.epoch or not seq.isdigit():
            return None
        return int(seq)

    async def add(self, key: uuid.UUID | str, event_type: str, data: dict[str, Any]) -> int:
        """
        Add an event to the ring of one key.

        Protected by asyncio.Lock to prevent interleaved mutations from
        concurrent coroutines.

        Args:
            key: Who receives it -- a mail or DAV account id, a user id,
                ADMIN_RING_KEY or EVERYONE_RING_KEY (auth/scope.py)
            event_type: SSE event type (sync.state, sync.progress, etc.)
            data: Event payload

        Returns:
            Sequence ID assigned to this event

        Raises:
            ValueError: event_type is not in SSE_EVENT_TYPES
        """
        if event_type not in SSE_EVENT_TYPES:
            raise ValueError(f"Unregistered SSE event type: {event_type!r}")
        async with self._lock:
            self._seq += 1
            seq_id = self._seq
            ring_key = str(key)

            event: dict[str, Any] = {
                "id": seq_id,
                "event_type": event_type,
                "data": data,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            ring = self._rings.setdefault(ring_key, deque(maxlen=self._max_size))
            if len(ring) == self._max_size:
                self._evicted[ring_key] = ring[0]["id"]
            ring.append(event)

            for waiter in self._waiters.get(ring_key, []):
                waiter.set()

            return seq_id

    async def replay_from(self, last_event_id: int, keys: Collection[str]) -> list[dict[str, Any]]:
        """
        The events of these keys after a given sequence ID, in order.

        Args:
            last_event_id: Last event ID the client received
            keys: The keys the client receives

        Returns:
            The events, oldest first; whether the list is complete is
            has_events_after()'s answer, not this one's
        """
        keys = _key_set(keys)
        async with self._lock:
            events = [
                event
                for key in keys
                for event in self._rings.get(key, ())
                if event["id"] > last_event_id
            ]
        events.sort(key=lambda e: e["id"])
        return events

    def has_events_after(self, last_event_id: int, keys: Collection[str]) -> bool:
        """
        Whether replaying these keys from last_event_id would be complete --
        no ring among them has dropped an event newer than that id.

        Every key counts, not just one: a quiet key whose ring still reaches
        back past last_event_id says nothing about a busier one that has
        already evicted ids in between, and replaying only the quiet one's
        events would leave the client believing it is caught up while it
        silently missed the busy key's gap.

        Args:
            last_event_id: Last event ID the client received
            keys: The keys the client receives
        """
        return all(self._evicted.get(key, 0) <= last_event_id for key in _key_set(keys))

    def get_latest_seq(self) -> int:
        """Get the latest global sequence number."""
        return self._seq

    def register_waiter(self, keys: Collection[str]) -> asyncio.Event:
        """
        Register a waiter that gets set when an event arrives on any of
        these keys, or when wake_all() is called.

        Args:
            keys: The keys to watch

        Returns:
            asyncio.Event that gets set when new events are added
        """
        waiter = asyncio.Event()
        for key in _key_set(keys):
            self._waiters.setdefault(key, []).append(waiter)
        return waiter

    def unregister_waiter(self, waiter: asyncio.Event, keys: Collection[str]) -> None:
        """
        Remove a previously registered waiter.

        Args:
            waiter: The asyncio.Event to remove
            keys: The keys it was registered with
        """
        for key in keys:
            waiters = self._waiters.get(key, [])
            try:
                waiters.remove(waiter)
            except ValueError:
                pass
            if not waiters:
                self._waiters.pop(key, None)

    def wake_all(self) -> None:
        """Set every registered waiter -- each stream looks at the ring and
        at what its caller owns again (see auth/ownership.py)."""
        for waiters in self._waiters.values():
            for waiter in waiters:
                waiter.set()

    def clear_account(self, account_id: str) -> None:
        """
        Clear all events for an account.

        Args:
            account_id: Account to clear events for
        """
        ring = self._rings.pop(account_id, None)
        if ring:
            self._evicted[account_id] = ring[-1]["id"]
