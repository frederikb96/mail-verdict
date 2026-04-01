"""
Async event bus for internal publish/subscribe messaging.

Simplified for PostIMAP mode: dispatches dict-based events from
PG LISTEN to registered subscribers by event key (e.g. "message", "folder").
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# Subscriber callback: receives an event dict, returns None
SubscriberCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


@dataclass
class Subscriber:
    """
    A named subscriber with a callback and priority level.

    Lower priority number = dispatched first (e.g. spam=10, rules=50, SSE=90).
    """

    name: str
    callback: SubscriberCallback
    priority: int = 50


@dataclass
class EventBus:
    """
    Async event bus dispatching PG LISTEN events to registered subscribers.

    Thread-safe via asyncio lock. Subscribers are called sequentially
    in priority order per event key.
    """

    _subscribers: dict[str, list[Subscriber]] = field(
        default_factory=lambda: defaultdict(list)
    )
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def subscribe(
        self,
        event_key: str,
        subscriber: Subscriber,
    ) -> None:
        """
        Register a subscriber for a specific event key.

        Args:
            event_key: Event key to subscribe to (e.g. "message", "folder")
            subscriber: Subscriber with callback and priority
        """
        async with self._lock:
            subs = self._subscribers[event_key]
            subs.append(subscriber)
            subs.sort(key=lambda s: s.priority)
            logger.debug(
                "Subscriber registered",
                extra={
                    "subscriber": subscriber.name,
                    "event_key": event_key,
                    "priority": subscriber.priority,
                },
            )

    async def unsubscribe(
        self,
        event_key: str,
        name: str,
    ) -> bool:
        """
        Remove a named subscriber from an event key.

        Args:
            event_key: The event key to unsubscribe from
            name: Subscriber name to remove

        Returns:
            True if subscriber was found and removed
        """
        async with self._lock:
            subs = self._subscribers[event_key]
            before = len(subs)
            self._subscribers[event_key] = [s for s in subs if s.name != name]
            return len(self._subscribers[event_key]) < before

    async def emit(self, event_key: str, event: dict[str, Any]) -> None:
        """
        Dispatch an event to all subscribers registered for its key.

        Subscribers are called sequentially in priority order.
        Individual subscriber failures are logged but do not stop dispatch.

        Args:
            event_key: Event key identifying the event type
            event: The event payload dict
        """
        async with self._lock:
            subs = list(self._subscribers.get(event_key, []))

        logger.debug(
            "Dispatching event",
            extra={"event_key": event_key, "subscriber_count": len(subs)},
        )

        results = await asyncio.gather(
            *[self._safe_dispatch(sub, event) for sub in subs],
            return_exceptions=True,
        )
        for sub, result in zip(subs, results):
            if isinstance(result, Exception):
                logger.exception(
                    "Subscriber raised exception",
                    extra={"subscriber": sub.name, "event_key": event_key},
                    exc_info=result,
                )

    @staticmethod
    async def _safe_dispatch(sub: Subscriber, event: dict[str, Any]) -> None:
        """Dispatch to a single subscriber with exception propagation."""
        await sub.callback(event)

    async def subscriber_count(self, event_key: str) -> int:
        """
        Get the number of subscribers for an event key.

        Args:
            event_key: The event key to check
        """
        async with self._lock:
            return len(self._subscribers.get(event_key, []))
