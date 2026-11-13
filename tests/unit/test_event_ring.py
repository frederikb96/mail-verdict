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

        id1 = await ring.add(account_id, "calendar.account", {"phase": "idle"})
        id2 = await ring.add(account_id, "calendar.links_changed", {"synced": 10})

        assert id1 == 1
        assert id2 == 2

    @pytest.mark.asyncio
    async def test_add_stores_event(self) -> None:
        """Added event is retrievable via replay."""
        ring = EventRing()
        account_id = uuid.uuid4()
        acct_str = str(account_id)

        await ring.add(account_id, "calendar.account", {"phase": "syncing"})

        # Direct access to ring internals for verification
        assert acct_str in ring._rings
        assert len(ring._rings[acct_str]) == 1
        event = ring._rings[acct_str][0]
        assert event["event_type"] == "calendar.account"
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
            await ring.add(account_id, "calendar.links_changed", {"synced": i})

        assert len(ring._rings[acct_str]) == 3
        # Oldest remaining should be ID 3
        assert ring._rings[acct_str][0]["id"] == 3

    @pytest.mark.asyncio
    async def test_add_separate_accounts(self) -> None:
        """Events for different accounts are stored separately."""
        ring = EventRing()
        acct1 = uuid.uuid4()
        acct2 = uuid.uuid4()

        await ring.add(acct1, "calendar.account", {"phase": "idle"})
        await ring.add(acct2, "calendar.account", {"phase": "syncing"})

        assert len(ring._rings[str(acct1)]) == 1
        assert len(ring._rings[str(acct2)]) == 1

    @pytest.mark.asyncio
    async def test_global_sequence_across_accounts(self) -> None:
        """Sequence IDs are global, not per-account."""
        ring = EventRing()
        acct1 = uuid.uuid4()
        acct2 = uuid.uuid4()

        id1 = await ring.add(acct1, "calendar.account", {"phase": "idle"})
        id2 = await ring.add(acct2, "calendar.account", {"phase": "syncing"})

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

        await ring.add(account_id, "calendar.account", {"phase": "idle"})
        await ring.add(account_id, "calendar.links_changed", {"synced": 10})
        await ring.add(account_id, "calendar.links_changed", {"synced": 20})

        events = await ring.replay_from(1, {acct_str})
        assert len(events) == 2
        assert events[0]["data"]["synced"] == 10
        assert events[1]["data"]["synced"] == 20

    @pytest.mark.asyncio
    async def test_replay_from_unknown_key(self) -> None:
        """replay_from() returns empty for a key nothing was added to."""
        ring = EventRing()
        events = await ring.replay_from(0, {str(uuid.uuid4())})
        assert events == []

    @pytest.mark.asyncio
    async def test_replay_merges_exactly_the_given_keys_in_order(self) -> None:
        """A replay over several keys returns each of their events once, by
        global id, and nothing of any key it was not given."""
        ring = EventRing()
        acct, user, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        await ring.add(acct, "mail.new", {"n": 1})
        await ring.add(other, "mail.new", {"n": "other"})
        await ring.add(user, "order.updated", {"n": 2})
        await ring.add(acct, "mail.new", {"n": 3})

        events = await ring.replay_from(0, {str(acct), str(user)})

        assert [e["data"]["n"] for e in events] == [1, 2, 3]
        assert [e["id"] for e in events] == [1, 3, 4]

    @pytest.mark.asyncio
    async def test_a_lone_string_is_refused_as_a_key_set(self) -> None:
        """A key passed as a bare string would iterate as one-character keys
        and match nothing -- a stream gone silent with no error."""
        ring = EventRing()
        account_id = uuid.uuid4()
        await ring.add(account_id, "mail.new", {})

        with pytest.raises(TypeError):
            await ring.replay_from(0, str(account_id))
        with pytest.raises(TypeError):
            ring.has_events_after(0, str(account_id))
        with pytest.raises(TypeError):
            ring.register_waiter(str(account_id))


class TestEventRingHasEventsAfter:
    """Tests for gap detection."""

    @pytest.mark.asyncio
    async def test_has_events_for_valid_id(self) -> None:
        """Returns True when the ID is within the ring."""
        ring = EventRing()
        account_id = uuid.uuid4()
        acct_str = str(account_id)

        await ring.add(account_id, "calendar.account", {"phase": "idle"})
        await ring.add(account_id, "calendar.links_changed", {"synced": 10})

        assert ring.has_events_after(1, {acct_str}) is True

    @pytest.mark.asyncio
    async def test_has_events_for_old_id(self) -> None:
        """Returns False when the ID has been evicted."""
        ring = EventRing(max_size=2)
        account_id = uuid.uuid4()
        acct_str = str(account_id)

        await ring.add(account_id, "calendar.account", {"phase": "idle"})
        await ring.add(account_id, "calendar.links_changed", {"synced": 10})
        await ring.add(account_id, "calendar.links_changed", {"synced": 20})
        # IDs 1 and 2 evicted: a client that saw ID 1 missed ID 2 for good;
        # one that saw ID 2 still gets everything after it.
        await ring.add(account_id, "calendar.links_changed", {"synced": 30})

        assert ring.has_events_after(1, {acct_str}) is False
        assert ring.has_events_after(2, {acct_str}) is True

    def test_has_events_unknown_key(self) -> None:
        """A key nothing was ever added to has nothing missing."""
        ring = EventRing()
        assert ring.has_events_after(0, {str(uuid.uuid4())}) is True

    @pytest.mark.asyncio
    async def test_a_key_whose_first_event_came_later_is_still_complete(self) -> None:
        """A ring that never dropped anything replays completely, even when
        its first event is newer than the client's id -- only an eviction
        makes a replay incomplete."""
        ring = EventRing(max_size=3)
        quiet, fresh = uuid.uuid4(), uuid.uuid4()

        last_event_id = await ring.add(quiet, "calendar.account", {"n": 1})
        await ring.add(fresh, "calendar.account", {"n": 2})

        assert ring.has_events_after(last_event_id, {str(quiet), str(fresh)}) is True

    @pytest.mark.asyncio
    async def test_check_is_defeated_by_a_busy_key_alone(self) -> None:
        """A quiet key whose ring still reaches back past last_event_id
        must not green-light a replay on its own -- a busier key of the
        same stream may have already evicted ids in between, and replaying
        only misses them silently."""
        ring = EventRing(max_size=3)
        quiet, busy = uuid.uuid4(), uuid.uuid4()

        # Client's last-seen id, from back when both keys were quiet.
        await ring.add(quiet, "calendar.account", {"n": 1})  # id 1
        last_event_id = await ring.add(busy, "calendar.account", {"n": 1})  # id 2

        # The busy key then evicts ids 2 through the ring's whole
        # capacity while the quiet one produces nothing further.
        for i in range(2, 6):
            await ring.add(busy, "calendar.links_changed", {"n": i})  # ids 3..6

        assert ring.has_events_after(last_event_id, {str(quiet)}) is True
        assert ring.has_events_after(last_event_id, {str(quiet), str(busy)}) is False

    @pytest.mark.asyncio
    async def test_another_streams_eviction_does_not_force_a_resync(self) -> None:
        """Only the stream's own keys count: a key it does not read having
        dropped events says nothing about its replay."""
        ring = EventRing(max_size=2)
        mine, theirs = uuid.uuid4(), uuid.uuid4()

        last_event_id = await ring.add(mine, "calendar.account", {"n": 1})
        for i in range(5):
            await ring.add(theirs, "calendar.account", {"n": i})

        assert ring.has_events_after(last_event_id, {str(mine)}) is True

    @pytest.mark.asyncio
    async def test_check_passes_when_every_key_still_covers_the_gap(self) -> None:
        """The ordinary case: nothing has been evicted anywhere, so a
        replay from last_event_id is complete."""
        ring = EventRing()
        acct_a, acct_b = uuid.uuid4(), uuid.uuid4()

        await ring.add(acct_a, "calendar.account", {"n": 1})  # id 1
        last_event_id = await ring.add(acct_b, "calendar.account", {"n": 1})  # id 2
        await ring.add(acct_a, "calendar.links_changed", {"n": 2})  # id 3, after last_event_id

        assert ring.has_events_after(last_event_id, {str(acct_a), str(acct_b)}) is True


class TestEventRingWaiters:
    """Tests for waiter notification."""

    @pytest.mark.asyncio
    async def test_waiter_notified_on_add(self) -> None:
        """Registered waiter gets set when event is added for that account."""
        ring = EventRing()
        account_id = uuid.uuid4()
        acct_str = str(account_id)

        waiter = ring.register_waiter({acct_str})
        assert not waiter.is_set()

        await ring.add(account_id, "calendar.account", {"phase": "idle"})
        assert waiter.is_set()

    @pytest.mark.asyncio
    async def test_waiter_notified_by_any_of_its_keys(self) -> None:
        """A waiter over several keys wakes for an event on any of them."""
        ring = EventRing()
        acct, user = uuid.uuid4(), uuid.uuid4()

        waiter = ring.register_waiter({str(acct), str(user)})
        await ring.add(user, "order.updated", {})

        assert waiter.is_set()

    @pytest.mark.asyncio
    async def test_wake_all_sets_every_waiter(self) -> None:
        """wake_all() wakes every stream, whatever its keys."""
        ring = EventRing()
        first = ring.register_waiter({str(uuid.uuid4())})
        second = ring.register_waiter({str(uuid.uuid4()), "admin"})

        ring.wake_all()

        assert first.is_set() and second.is_set()

    @pytest.mark.asyncio
    async def test_unregister_waiter(self) -> None:
        """Unregistered waiter is no longer notified."""
        ring = EventRing()
        account_id = uuid.uuid4()
        acct_str = str(account_id)

        waiter = ring.register_waiter({acct_str})
        ring.unregister_waiter(waiter, {acct_str})

        await ring.add(account_id, "calendar.account", {"phase": "idle"})
        assert not waiter.is_set()

    @pytest.mark.asyncio
    async def test_unregister_unknown_waiter(self) -> None:
        """Unregistering an unknown waiter does not raise."""
        ring = EventRing()
        waiter = asyncio.Event()
        ring.unregister_waiter(waiter, {str(uuid.uuid4())})  # Should not raise


class TestEventRingClearAccount:
    """Tests for clearing account events."""

    @pytest.mark.asyncio
    async def test_clear_removes_events(self) -> None:
        """clear_account() removes all events for that account."""
        ring = EventRing()
        acct1 = uuid.uuid4()
        acct2 = uuid.uuid4()

        await ring.add(acct1, "calendar.account", {"phase": "idle"})
        await ring.add(acct2, "calendar.account", {"phase": "idle"})

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
        await ring.add(uuid.uuid4(), "calendar.account", {"phase": "idle"})
        assert ring.get_latest_seq() == 1
        await ring.add(uuid.uuid4(), "calendar.account", {"phase": "idle"})


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
        events_a = await ring.replay_from(id_a1, {str(acct_a)})
        events_b = await ring.replay_from(id_b1, {str(acct_b)})

        # Account A: only uid=2 (after id_a1)
        assert len(events_a) == 1
        assert events_a[0]["data"]["uid"] == 2

        # Account B: no events after id_b1
        assert len(events_b) == 0

        # Account B never sees account A's events, from any id
        all_b = await ring.replay_from(0, {str(acct_b)})
        assert [e["data"]["uid"] for e in all_b] == [100]

    @pytest.mark.asyncio
    async def test_waiter_not_notified_for_other_account(self) -> None:
        """A waiter registered for account A is not triggered by account B events."""
        ring = EventRing()
        acct_a = uuid.uuid4()
        acct_b = uuid.uuid4()

        waiter_a = ring.register_waiter({str(acct_a)})
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
        events_b = await ring.replay_from(id_b, {str(acct_b)})
        # id_b is the only event, replay_from returns events AFTER id_b
        assert len(events_b) == 0
        assert str(acct_b) in ring._rings
        assert len(ring._rings[str(acct_b)]) == 1
        assert ring.get_latest_seq() == 2
