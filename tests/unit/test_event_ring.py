"""Tests for EventRing: add, replay, waiter notification."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from mail_verdict.api.event_ring import EventRing


class TestEventRingAdd:
    """Tests for adding events to the ring buffer."""

    @pytest.mark.asyncio
    async def test_add_returns_sequence_id(self) -> None:
        """add() returns a monotonically increasing sequence ID."""
        ring = EventRing()
        account_id = uuid.uuid4()

        id1 = await ring.add(account_id, "sync.state", {"phase": "idle"})
        id2 = await ring.add(account_id, "sync.progress", {"synced": 10})

        assert id1 == 1
        assert id2 == 2

    @pytest.mark.asyncio
    async def test_add_stores_event(self) -> None:
        """Added event is retrievable via replay."""
        ring = EventRing()
        account_id = uuid.uuid4()
        acct_str = str(account_id)

        await ring.add(account_id, "sync.state", {"phase": "syncing"})

        # Direct access to ring internals for verification
        assert acct_str in ring._rings
        assert len(ring._rings[acct_str]) == 1
        event = ring._rings[acct_str][0]
        assert event["event_type"] == "sync.state"
        assert event["data"]["phase"] == "syncing"
        assert event["id"] == 1
        assert "timestamp" in event

    @pytest.mark.asyncio
    async def test_add_respects_max_size(self) -> None:
        """Ring buffer evicts oldest events when full."""
        ring = EventRing(max_size=3)
        account_id = uuid.uuid4()
        acct_str = str(account_id)

        for i in range(5):
            await ring.add(account_id, "sync.progress", {"synced": i})

        assert len(ring._rings[acct_str]) == 3
        # Oldest remaining should be ID 3
        assert ring._rings[acct_str][0]["id"] == 3

    @pytest.mark.asyncio
    async def test_add_separate_accounts(self) -> None:
        """Events for different accounts are stored separately."""
        ring = EventRing()
        acct1 = uuid.uuid4()
        acct2 = uuid.uuid4()

        await ring.add(acct1, "sync.state", {"phase": "idle"})
        await ring.add(acct2, "sync.state", {"phase": "syncing"})

        assert len(ring._rings[str(acct1)]) == 1
        assert len(ring._rings[str(acct2)]) == 1

    @pytest.mark.asyncio
    async def test_global_sequence_across_accounts(self) -> None:
        """Sequence IDs are global, not per-account."""
        ring = EventRing()
        acct1 = uuid.uuid4()
        acct2 = uuid.uuid4()

        id1 = await ring.add(acct1, "sync.state", {"phase": "idle"})
        id2 = await ring.add(acct2, "sync.state", {"phase": "syncing"})

        assert id1 == 1
        assert id2 == 2


class TestEventRingReplay:
    """Tests for replaying events."""

    @pytest.mark.asyncio
    async def test_replay_from_returns_newer(self) -> None:
        """replay_from() returns only events after the given ID."""
        ring = EventRing()
        account_id = uuid.uuid4()
        acct_str = str(account_id)

        await ring.add(account_id, "sync.state", {"phase": "idle"})
        await ring.add(account_id, "sync.progress", {"synced": 10})
        await ring.add(account_id, "sync.progress", {"synced": 20})

        events = await ring.replay_from(1, acct_str)
        assert len(events) == 2
        assert events[0]["data"]["synced"] == 10
        assert events[1]["data"]["synced"] == 20

    @pytest.mark.asyncio
    async def test_replay_from_returns_empty_for_gap(self) -> None:
        """replay_from() returns empty if the ID is too old (fell off ring)."""
        ring = EventRing(max_size=3)
        account_id = uuid.uuid4()
        acct_str = str(account_id)

        for i in range(5):
            await ring.add(account_id, "sync.progress", {"synced": i})

        # ID 1 is no longer in the ring (evicted)
        events = await ring.replay_from(1, acct_str)
        assert events == []

    @pytest.mark.asyncio
    async def test_replay_from_unknown_account(self) -> None:
        """replay_from() returns empty for unknown account."""
        ring = EventRing()
        events = await ring.replay_from(0, str(uuid.uuid4()))
        assert events == []

    @pytest.mark.asyncio
    async def test_replay_all_accounts(self) -> None:
        """replay_from() without account_id replays across all accounts."""
        ring = EventRing()
        acct1 = uuid.uuid4()
        acct2 = uuid.uuid4()

        await ring.add(acct1, "sync.state", {"phase": "idle"})
        await ring.add(acct2, "sync.state", {"phase": "syncing"})
        await ring.add(acct1, "sync.progress", {"synced": 10})

        events = await ring.replay_from(0)
        assert len(events) == 3
        # Should be sorted by global ID
        assert events[0]["id"] == 1
        assert events[1]["id"] == 2
        assert events[2]["id"] == 3


class TestEventRingHasEventsAfter:
    """Tests for gap detection."""

    @pytest.mark.asyncio
    async def test_has_events_for_valid_id(self) -> None:
        """Returns True when the ID is within the ring."""
        ring = EventRing()
        account_id = uuid.uuid4()
        acct_str = str(account_id)

        await ring.add(account_id, "sync.state", {"phase": "idle"})
        await ring.add(account_id, "sync.progress", {"synced": 10})

        assert ring.has_events_after(1, acct_str) is True

    @pytest.mark.asyncio
    async def test_has_events_for_old_id(self) -> None:
        """Returns False when the ID has been evicted."""
        ring = EventRing(max_size=2)
        account_id = uuid.uuid4()
        acct_str = str(account_id)

        await ring.add(account_id, "sync.state", {"phase": "idle"})
        await ring.add(account_id, "sync.progress", {"synced": 10})
        await ring.add(account_id, "sync.progress", {"synced": 20})

        # ID 1 evicted, oldest is ID 2
        assert ring.has_events_after(1, acct_str) is False

    def test_has_events_unknown_account(self) -> None:
        """Returns False for unknown account."""
        ring = EventRing()
        assert ring.has_events_after(0, str(uuid.uuid4())) is False


class TestEventRingWaiters:
    """Tests for waiter notification."""

    @pytest.mark.asyncio
    async def test_waiter_notified_on_add(self) -> None:
        """Registered waiter gets set when event is added for that account."""
        ring = EventRing()
        account_id = uuid.uuid4()
        acct_str = str(account_id)

        waiter = ring.register_waiter(acct_str)
        assert not waiter.is_set()

        await ring.add(account_id, "sync.state", {"phase": "idle"})
        assert waiter.is_set()

    @pytest.mark.asyncio
    async def test_global_waiter_notified(self) -> None:
        """Global waiter (no account) gets set on any event."""
        ring = EventRing()
        account_id = uuid.uuid4()

        waiter = ring.register_waiter()
        assert not waiter.is_set()

        await ring.add(account_id, "sync.state", {"phase": "idle"})
        assert waiter.is_set()

    @pytest.mark.asyncio
    async def test_unregister_waiter(self) -> None:
        """Unregistered waiter is no longer notified."""
        ring = EventRing()
        account_id = uuid.uuid4()
        acct_str = str(account_id)

        waiter = ring.register_waiter(acct_str)
        ring.unregister_waiter(waiter, acct_str)

        await ring.add(account_id, "sync.state", {"phase": "idle"})
        assert not waiter.is_set()

    @pytest.mark.asyncio
    async def test_unregister_unknown_waiter(self) -> None:
        """Unregistering an unknown waiter does not raise."""
        ring = EventRing()
        waiter = asyncio.Event()
        ring.unregister_waiter(waiter)  # Should not raise


class TestEventRingClearAccount:
    """Tests for clearing account events."""

    @pytest.mark.asyncio
    async def test_clear_removes_events(self) -> None:
        """clear_account() removes all events for that account."""
        ring = EventRing()
        acct1 = uuid.uuid4()
        acct2 = uuid.uuid4()

        await ring.add(acct1, "sync.state", {"phase": "idle"})
        await ring.add(acct2, "sync.state", {"phase": "idle"})

        ring.clear_account(str(acct1))
        assert str(acct1) not in ring._rings
        assert str(acct2) in ring._rings

    def test_clear_unknown_account(self) -> None:
        """clear_account() for unknown account does not raise."""
        ring = EventRing()
        ring.clear_account(str(uuid.uuid4()))


class TestEventRingLatestSeq:
    """Tests for sequence tracking."""

    def test_latest_seq_starts_zero(self) -> None:
        """Latest seq is 0 initially."""
        ring = EventRing()
        assert ring.get_latest_seq() == 0

    @pytest.mark.asyncio
    async def test_latest_seq_increases(self) -> None:
        """Latest seq increases with each add."""
        ring = EventRing()
        await ring.add(uuid.uuid4(), "sync.state", {"phase": "idle"})
        assert ring.get_latest_seq() == 1
        await ring.add(uuid.uuid4(), "sync.state", {"phase": "idle"})


class TestEventRingAccountIsolation:
    """Tests confirming events for one account never leak to another."""

    @pytest.mark.asyncio
    async def test_replay_filters_by_account(self) -> None:
        """Events for account A are not returned when replaying account B."""
        ring = EventRing()
        acct_a = uuid.uuid4()
        acct_b = uuid.uuid4()

        id_a1 = await ring.add(acct_a, "mail.new", {"uid": 1})
        await ring.add(acct_a, "mail.new", {"uid": 2})
        id_b1 = await ring.add(acct_b, "mail.new", {"uid": 100})

        # Replay from the first event's ID to get subsequent events
        events_a = await ring.replay_from(id_a1, str(acct_a))
        events_b = await ring.replay_from(id_b1, str(acct_b))

        # Account A: only uid=2 (after id_a1)
        assert len(events_a) == 1
        assert events_a[0]["data"]["uid"] == 2

        # Account B: no events after id_b1
        assert len(events_b) == 0

        # Verify account B never sees account A events
        all_b = await ring.replay_from(id_b1 - 1, str(acct_b))
        assert len(all_b) == 0  # id_b1-1 < oldest_id in B's ring, gap detected

        # Verify global replay shows all accounts
        global_events = await ring.replay_from(id_a1, None)
        assert len(global_events) == 2  # uid=2 from A, uid=100 from B

    @pytest.mark.asyncio
    async def test_waiter_not_notified_for_other_account(self) -> None:
        """A waiter registered for account A is not triggered by account B events."""
        ring = EventRing()
        acct_a = uuid.uuid4()
        acct_b = uuid.uuid4()

        waiter_a = ring.register_waiter(str(acct_a))
        assert not waiter_a.is_set()

        await ring.add(acct_b, "mail.new", {"uid": 1})
        assert not waiter_a.is_set()

        await ring.add(acct_a, "mail.new", {"uid": 2})
        assert waiter_a.is_set()

    @pytest.mark.asyncio
    async def test_clear_one_account_preserves_other(self) -> None:
        """Clearing account A events does not affect account B."""
        ring = EventRing()
        acct_a = uuid.uuid4()
        acct_b = uuid.uuid4()

        await ring.add(acct_a, "mail.new", {"uid": 1})
        id_b = await ring.add(acct_b, "mail.new", {"uid": 2})

        ring.clear_account(str(acct_a))

        # Account A ring is empty
        assert str(acct_a) not in ring._rings

        # Account B ring still has its event
        events_b = await ring.replay_from(id_b, str(acct_b))
        # id_b is the only event, replay_from returns events AFTER id_b
        assert len(events_b) == 0
        assert str(acct_b) in ring._rings
        assert len(ring._rings[str(acct_b)]) == 1
        assert ring.get_latest_seq() == 2
