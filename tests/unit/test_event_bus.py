"""Tests for EventBus: dispatch, subscriber isolation, priority ordering."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from mail_verdict.rules.bus import EventBus, Subscriber


def _make_event(**kwargs: object) -> dict[str, Any]:
    """Create a test event dict."""
    defaults: dict[str, object] = {
        "op": "insert",
        "id": str(uuid.uuid4()),
        "account_id": str(uuid.uuid4()),
        "folder_id": str(uuid.uuid4()),
    }
    defaults.update(kwargs)
    return defaults


class TestSubscribeUnsubscribe:
    """Tests for subscribe/unsubscribe."""

    @pytest.mark.asyncio
    async def test_subscribe_adds_subscriber(self) -> None:
        """Subscriber is registered for event key."""
        bus = EventBus()
        cb = AsyncMock()
        await bus.subscribe("message", Subscriber(name="test", callback=cb))
        assert await bus.subscriber_count("message") == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_removes(self) -> None:
        """Subscriber is removed by name."""
        bus = EventBus()
        cb = AsyncMock()
        await bus.subscribe("message", Subscriber(name="test", callback=cb))
        removed = await bus.unsubscribe("message", "test")
        assert removed is True
        assert await bus.subscriber_count("message") == 0

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent(self) -> None:
        """Removing non-existent subscriber returns False."""
        bus = EventBus()
        removed = await bus.unsubscribe("message", "ghost")
        assert removed is False


class TestEmit:
    """Tests for event emission."""

    @pytest.mark.asyncio
    async def test_dispatches_to_subscriber(self) -> None:
        """Emit dispatches event to registered subscriber."""
        bus = EventBus()
        cb = AsyncMock()
        await bus.subscribe("message", Subscriber(name="test", callback=cb))

        event = _make_event()
        await bus.emit("message", event)
        cb.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_isolation_between_event_keys(self) -> None:
        """Subscriber for 'message' is not called for 'folder'."""
        bus = EventBus()
        cb = AsyncMock()
        await bus.subscribe("message", Subscriber(name="test", callback=cb))

        event = _make_event()
        await bus.emit("folder", event)
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_priority_ordering(self) -> None:
        """Subscribers are called in priority order (lower first)."""
        bus = EventBus()
        call_order: list[str] = []

        async def cb_high(event: dict[str, Any]) -> None:
            call_order.append("high")

        async def cb_low(event: dict[str, Any]) -> None:
            call_order.append("low")

        await bus.subscribe("message", Subscriber(name="low", callback=cb_low, priority=10))
        await bus.subscribe("message", Subscriber(name="high", callback=cb_high, priority=90))

        await bus.emit("message", _make_event())
        assert call_order == ["low", "high"]

    @pytest.mark.asyncio
    async def test_subscriber_exception_doesnt_stop_others(self) -> None:
        """A failing subscriber doesn't prevent subsequent ones from running."""
        bus = EventBus()
        cb_fail = AsyncMock(side_effect=RuntimeError("boom"))
        cb_ok = AsyncMock()

        await bus.subscribe("message", Subscriber(name="fail", callback=cb_fail, priority=10))
        await bus.subscribe("message", Subscriber(name="ok", callback=cb_ok, priority=20))

        await bus.emit("message", _make_event())
        cb_ok.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_subscribers_is_ok(self) -> None:
        """Emit with no subscribers is a no-op."""
        bus = EventBus()
        await bus.emit("message", _make_event())

    @pytest.mark.asyncio
    async def test_multiple_subscribers_all_called(self) -> None:
        """All subscribers for an event key are called."""
        bus = EventBus()
        cb1 = AsyncMock()
        cb2 = AsyncMock()
        await bus.subscribe("message", Subscriber(name="s1", callback=cb1))
        await bus.subscribe("message", Subscriber(name="s2", callback=cb2))

        await bus.emit("message", _make_event())
        cb1.assert_awaited_once()
        cb2.assert_awaited_once()
