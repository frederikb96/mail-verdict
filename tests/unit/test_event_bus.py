"""Tests for EventBus: dispatch, subscriber isolation, priority ordering."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from mail_verdict.rules.bus import EventBus, Subscriber
from mail_verdict.sync.events import (
    FlagsChanged,
    MailDeleted,
    MailReceived,
    SyncEvent,
)


def _make_event(event_cls: type[SyncEvent] = MailReceived, **kwargs: object) -> SyncEvent:
    """Create a test event."""
    defaults: dict[str, object] = {
        "account_id": uuid.uuid4(),
        "folder_id": uuid.uuid4(),
    }
    defaults.update(kwargs)
    return event_cls(**defaults)  # type: ignore[arg-type]


class TestSubscribeUnsubscribe:
    """Tests for subscribe/unsubscribe."""

    @pytest.mark.asyncio
    async def test_subscribe_adds_subscriber(self) -> None:
        """Subscriber is registered for event type."""
        bus = EventBus()
        cb = AsyncMock()
        await bus.subscribe(MailReceived, Subscriber(name="test", callback=cb))
        assert await bus.subscriber_count(MailReceived) == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_removes(self) -> None:
        """Subscriber is removed by name."""
        bus = EventBus()
        cb = AsyncMock()
        await bus.subscribe(MailReceived, Subscriber(name="test", callback=cb))
        removed = await bus.unsubscribe(MailReceived, "test")
        assert removed is True
        assert await bus.subscriber_count(MailReceived) == 0

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent(self) -> None:
        """Removing non-existent subscriber returns False."""
        bus = EventBus()
        removed = await bus.unsubscribe(MailReceived, "ghost")
        assert removed is False


class TestEmit:
    """Tests for event emission."""

    @pytest.mark.asyncio
    async def test_dispatches_to_subscriber(self) -> None:
        """Emit dispatches event to registered subscriber."""
        bus = EventBus()
        cb = AsyncMock()
        await bus.subscribe(MailReceived, Subscriber(name="test", callback=cb))

        event = _make_event(MailReceived, uid=1)
        await bus.emit(event)
        cb.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_isolation_between_event_types(self) -> None:
        """Subscriber for MailReceived is not called for MailDeleted."""
        bus = EventBus()
        cb = AsyncMock()
        await bus.subscribe(MailReceived, Subscriber(name="test", callback=cb))

        event = _make_event(MailDeleted, uid=1)
        await bus.emit(event)
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_priority_ordering(self) -> None:
        """Subscribers are called in priority order (lower first)."""
        bus = EventBus()
        call_order: list[str] = []

        async def cb_high(event: SyncEvent) -> None:
            call_order.append("high")

        async def cb_low(event: SyncEvent) -> None:
            call_order.append("low")

        await bus.subscribe(MailReceived, Subscriber(name="low", callback=cb_low, priority=10))
        await bus.subscribe(MailReceived, Subscriber(name="high", callback=cb_high, priority=90))

        await bus.emit(_make_event(MailReceived, uid=1))
        assert call_order == ["low", "high"]

    @pytest.mark.asyncio
    async def test_subscriber_exception_doesnt_stop_others(self) -> None:
        """A failing subscriber doesn't prevent subsequent ones from running."""
        bus = EventBus()
        cb_fail = AsyncMock(side_effect=RuntimeError("boom"))
        cb_ok = AsyncMock()

        await bus.subscribe(MailReceived, Subscriber(name="fail", callback=cb_fail, priority=10))
        await bus.subscribe(MailReceived, Subscriber(name="ok", callback=cb_ok, priority=20))

        await bus.emit(_make_event(MailReceived, uid=1))
        cb_ok.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_subscribers_is_ok(self) -> None:
        """Emit with no subscribers is a no-op."""
        bus = EventBus()
        await bus.emit(_make_event(MailReceived, uid=1))

    @pytest.mark.asyncio
    async def test_multiple_subscribers_all_called(self) -> None:
        """All subscribers for an event type are called."""
        bus = EventBus()
        cb1 = AsyncMock()
        cb2 = AsyncMock()
        await bus.subscribe(MailReceived, Subscriber(name="s1", callback=cb1))
        await bus.subscribe(MailReceived, Subscriber(name="s2", callback=cb2))

        await bus.emit(_make_event(MailReceived, uid=1))
        cb1.assert_awaited_once()
        cb2.assert_awaited_once()
