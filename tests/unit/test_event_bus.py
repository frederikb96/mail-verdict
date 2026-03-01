"""
Unit tests for event bus: dispatch, subscriber isolation, priority.

Row 153 (o=10.45): subscribe, emit, priority ordering,
multiple subscribers, error isolation.
"""

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

pytestmark = pytest.mark.unit


@pytest.fixture
def bus() -> EventBus:
    """Fresh event bus for each test."""
    return EventBus()


@pytest.fixture
def sample_event() -> MailReceived:
    """Sample MailReceived event."""
    return MailReceived(
        account_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        folder_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        uid=42,
    )


class TestSubscribe:
    """Tests for subscriber registration."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_subscribe_adds_subscriber(self, bus: EventBus) -> None:
        """Subscribing increases subscriber count."""
        cb = AsyncMock()
        await bus.subscribe(MailReceived, Subscriber(name="test", callback=cb))
        count = await bus.subscriber_count(MailReceived)
        assert count == 1

    @pytest.mark.asyncio(loop_scope="function")
    async def test_subscribe_multiple_types(self, bus: EventBus) -> None:
        """Subscribers for different event types are independent."""
        cb = AsyncMock()
        await bus.subscribe(MailReceived, Subscriber(name="test1", callback=cb))
        await bus.subscribe(MailDeleted, Subscriber(name="test2", callback=cb))

        assert await bus.subscriber_count(MailReceived) == 1
        assert await bus.subscriber_count(MailDeleted) == 1
        assert await bus.subscriber_count(FlagsChanged) == 0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_unsubscribe(self, bus: EventBus) -> None:
        """Unsubscribing removes the subscriber."""
        cb = AsyncMock()
        await bus.subscribe(MailReceived, Subscriber(name="test", callback=cb))
        removed = await bus.unsubscribe(MailReceived, "test")
        assert removed is True
        assert await bus.subscriber_count(MailReceived) == 0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_unsubscribe_nonexistent(self, bus: EventBus) -> None:
        """Unsubscribing a nonexistent subscriber returns False."""
        removed = await bus.unsubscribe(MailReceived, "nonexistent")
        assert removed is False


class TestEmit:
    """Tests for event dispatch."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_emit_calls_subscriber(self, bus: EventBus, sample_event: MailReceived) -> None:
        """Emit calls the subscriber callback."""
        cb = AsyncMock()
        await bus.subscribe(MailReceived, Subscriber(name="test", callback=cb))
        await bus.emit(sample_event)
        cb.assert_called_once_with(sample_event)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_emit_no_subscribers(self, bus: EventBus, sample_event: MailReceived) -> None:
        """Emit with no subscribers completes without error."""
        await bus.emit(sample_event)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_emit_wrong_type_not_called(
        self, bus: EventBus, sample_event: MailReceived
    ) -> None:
        """Subscribers for other event types are not called."""
        cb = AsyncMock()
        await bus.subscribe(MailDeleted, Subscriber(name="test", callback=cb))
        await bus.emit(sample_event)  # MailReceived, not MailDeleted
        cb.assert_not_called()


class TestPriority:
    """Tests for priority-ordered dispatch."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_priority_ordering(self, bus: EventBus, sample_event: MailReceived) -> None:
        """Subscribers are called in ascending priority order."""
        call_order: list[str] = []

        async def cb_high(event: SyncEvent) -> None:
            call_order.append("high")

        async def cb_low(event: SyncEvent) -> None:
            call_order.append("low")

        async def cb_medium(event: SyncEvent) -> None:
            call_order.append("medium")

        await bus.subscribe(MailReceived, Subscriber(name="low", callback=cb_low, priority=90))
        await bus.subscribe(MailReceived, Subscriber(name="high", callback=cb_high, priority=10))
        await bus.subscribe(
            MailReceived, Subscriber(name="medium", callback=cb_medium, priority=50)
        )

        await bus.emit(sample_event)
        assert call_order == ["high", "medium", "low"]


class TestMultipleSubscribers:
    """Tests for multiple subscriber behavior."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_all_subscribers_called(self, bus: EventBus, sample_event: MailReceived) -> None:
        """All subscribers for an event type are called."""
        cb1 = AsyncMock()
        cb2 = AsyncMock()
        cb3 = AsyncMock()

        await bus.subscribe(MailReceived, Subscriber(name="s1", callback=cb1))
        await bus.subscribe(MailReceived, Subscriber(name="s2", callback=cb2))
        await bus.subscribe(MailReceived, Subscriber(name="s3", callback=cb3))

        await bus.emit(sample_event)

        cb1.assert_called_once()
        cb2.assert_called_once()
        cb3.assert_called_once()


class TestErrorIsolation:
    """Tests for subscriber error isolation."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_failing_subscriber_doesnt_block_others(
        self, bus: EventBus, sample_event: MailReceived
    ) -> None:
        """A failing subscriber does not prevent subsequent subscribers from being called."""
        call_order: list[str] = []

        async def cb_fail(event: SyncEvent) -> None:
            call_order.append("fail")
            raise RuntimeError("I broke")

        async def cb_success(event: SyncEvent) -> None:
            call_order.append("success")

        await bus.subscribe(MailReceived, Subscriber(name="fail", callback=cb_fail, priority=10))
        await bus.subscribe(
            MailReceived, Subscriber(name="success", callback=cb_success, priority=50)
        )

        await bus.emit(sample_event)
        assert call_order == ["fail", "success"]

    @pytest.mark.asyncio(loop_scope="function")
    async def test_all_subscribers_called_despite_errors(
        self, bus: EventBus, sample_event: MailReceived
    ) -> None:
        """Even with multiple failures, all subscribers are attempted."""
        cb1 = AsyncMock(side_effect=RuntimeError("error 1"))
        cb2 = AsyncMock(side_effect=RuntimeError("error 2"))
        cb3 = AsyncMock()

        await bus.subscribe(MailReceived, Subscriber(name="s1", callback=cb1, priority=10))
        await bus.subscribe(MailReceived, Subscriber(name="s2", callback=cb2, priority=20))
        await bus.subscribe(MailReceived, Subscriber(name="s3", callback=cb3, priority=30))

        await bus.emit(sample_event)

        cb1.assert_called_once()
        cb2.assert_called_once()
        cb3.assert_called_once()
