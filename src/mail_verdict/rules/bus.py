"""
Async event bus for internal publish/subscribe messaging.

All mail state changes emit events through this bus.
Subscribers register by event type and are dispatched in priority order.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from mail_verdict.sync.events import SyncEvent

logger = logging.getLogger(__name__)

# Subscriber callback: receives a SyncEvent, returns None
SubscriberCallback = Callable[[SyncEvent], Coroutine[Any, Any, None]]


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
    Async event bus dispatching SyncEvents to registered subscribers.

    Thread-safe via asyncio lock. Subscribers are called sequentially
    in priority order per event type.
    """

    _subscribers: dict[type[SyncEvent], list[Subscriber]] = field(
        default_factory=lambda: defaultdict(list)
    )
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def subscribe(
        self,
        event_type: type[SyncEvent],
        subscriber: Subscriber,
    ) -> None:
        """
        Register a subscriber for a specific event type.

        Args:
            event_type: The SyncEvent subclass to subscribe to
            subscriber: Subscriber with callback and priority
        """
        async with self._lock:
            subs = self._subscribers[event_type]
            subs.append(subscriber)
            subs.sort(key=lambda s: s.priority)
            logger.debug(
                "Subscriber registered",
                extra={
                    "subscriber": subscriber.name,
                    "event_type": event_type.__name__,
                    "priority": subscriber.priority,
                },
            )

    async def unsubscribe(
        self,
        event_type: type[SyncEvent],
        name: str,
    ) -> bool:
        """
        Remove a named subscriber from an event type.

        Args:
            event_type: The event type to unsubscribe from
            name: Subscriber name to remove

        Returns:
            True if subscriber was found and removed
        """
        async with self._lock:
            subs = self._subscribers[event_type]
            before = len(subs)
            self._subscribers[event_type] = [s for s in subs if s.name != name]
            return len(self._subscribers[event_type]) < before

    async def emit(self, event: SyncEvent) -> None:
        """
        Dispatch an event to all subscribers registered for its type.

        Subscribers are called sequentially in priority order.
        Individual subscriber failures are logged but do not stop dispatch.

        Args:
            event: The event to dispatch
        """
        async with self._lock:
            subs = list(self._subscribers.get(type(event), []))

        event_name = type(event).__name__
        logger.debug(
            "Dispatching event",
            extra={"event": event_name, "subscriber_count": len(subs)},
        )

        for sub in subs:
            try:
                await sub.callback(event)
            except Exception:
                logger.exception(
                    "Subscriber raised exception",
                    extra={
                        "subscriber": sub.name,
                        "event": event_name,
                    },
                )

    async def subscriber_count(self, event_type: type[SyncEvent]) -> int:
        """
        Get the number of subscribers for an event type.

        Args:
            event_type: The event type to check
        """
        async with self._lock:
            return len(self._subscribers.get(event_type, []))
