"""
In-memory ring buffer for SSE events.

Stores the last N events per account with monotonic sequence IDs.
Supports replay from a given Last-Event-ID for reconnecting clients
and snapshot delivery for fresh connections.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_RING_SIZE = 500


class EventRing:
    """
    Per-account ring buffer of SSE events with global sequence counter.

    Thread-safe via asyncio.Lock for concurrent add/replay operations.
    Each event gets a monotonically increasing ID used by SSE Last-Event-ID.
    """

    def __init__(self, max_size: int = DEFAULT_RING_SIZE) -> None:
        """
        Initialize the event ring.

        Args:
            max_size: Maximum events to retain per account
        """
        self._max_size = max_size
        self._lock = asyncio.Lock()
        self._seq: int = 0
        self._rings: dict[str, deque[dict[str, Any]]] = {}
        self._waiters: dict[str, list[asyncio.Event]] = {}

    async def add(self, account_id: uuid.UUID, event_type: str, data: dict[str, Any]) -> int:
        """
        Add an event to the ring buffer for an account.

        Protected by asyncio.Lock to prevent interleaved mutations from
        concurrent coroutines.

        Args:
            account_id: Account the event belongs to
            event_type: SSE event type (sync.state, sync.progress, etc.)
            data: Event payload

        Returns:
            Sequence ID assigned to this event
        """
        async with self._lock:
            self._seq += 1
            seq_id = self._seq
            acct_key = str(account_id)

            event: dict[str, Any] = {
                "id": seq_id,
                "event_type": event_type,
                "data": data,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            if acct_key not in self._rings:
                self._rings[acct_key] = deque(maxlen=self._max_size)

            self._rings[acct_key].append(event)

            # Wake any waiting SSE generators for this account
            for waiter in self._waiters.get(acct_key, []):
                waiter.set()
            # Also wake global waiters (no account filter)
            for waiter in self._waiters.get("__global__", []):
                waiter.set()

            return seq_id

    async def replay_from(
        self,
        last_event_id: int,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Replay events after a given sequence ID.

        Args:
            last_event_id: Last event ID the client received
            account_id: Optional account filter (None = all accounts)

        Returns:
            List of events after last_event_id, or empty if gap too large
        """
        async with self._lock:
            events: list[dict[str, Any]] = []

            if account_id:
                ring = self._rings.get(account_id, deque())
                if not ring:
                    return []
                # Check if last_event_id is still in the ring
                oldest_id = ring[0]["id"]
                if last_event_id < oldest_id:
                    # Gap too large — caller should send full snapshot
                    return []
                for event in ring:
                    if event["id"] > last_event_id:
                        events.append(event)
            else:
                # Replay across all accounts
                for ring in self._rings.values():
                    for event in ring:
                        if event["id"] > last_event_id:
                            events.append(event)
                events.sort(key=lambda e: e["id"])

            return events

    def has_events_after(self, last_event_id: int, account_id: str | None = None) -> bool:
        """
        Check if there are events after a given ID (gap detection).

        Args:
            last_event_id: Last event ID the client received
            account_id: Optional account filter

        Returns:
            True if the ID is still within the ring (replay possible)
        """
        if account_id:
            ring = self._rings.get(account_id, deque())
            if not ring:
                return False
            return bool(ring[0]["id"] <= last_event_id)
        return bool(any(
            ring and ring[0]["id"] <= last_event_id
            for ring in self._rings.values()
        ))

    def get_latest_seq(self) -> int:
        """Get the latest global sequence number."""
        return self._seq

    def register_waiter(self, account_id: str | None = None) -> asyncio.Event:
        """
        Register a waiter that gets notified when new events arrive.

        Args:
            account_id: Account to watch, or None for all accounts

        Returns:
            asyncio.Event that gets set when new events are added
        """
        key = account_id if account_id else "__global__"
        waiter = asyncio.Event()
        if key not in self._waiters:
            self._waiters[key] = []
        self._waiters[key].append(waiter)
        return waiter

    def unregister_waiter(self, waiter: asyncio.Event, account_id: str | None = None) -> None:
        """
        Remove a previously registered waiter.

        Args:
            waiter: The asyncio.Event to remove
            account_id: Account key used during registration
        """
        key = account_id if account_id else "__global__"
        waiters = self._waiters.get(key, [])
        try:
            waiters.remove(waiter)
        except ValueError:
            pass
        if not waiters:
            self._waiters.pop(key, None)

    def clear_account(self, account_id: str) -> None:
        """
        Clear all events for an account.

        Args:
            account_id: Account to clear events for
        """
        self._rings.pop(account_id, None)
