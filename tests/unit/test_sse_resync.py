"""Tests for the SSE generator's reconnect-gap handling."""

from __future__ import annotations

import uuid

import pytest

import mail_verdict.api.events as events_module
from mail_verdict.api.event_ring import EventRing
from mail_verdict.api.events import _sse_generator


class _DisconnectedRequest:
    """Stub Request that reports disconnected on the first poll.

    Ends the generator's live-streaming loop immediately after it has
    emitted whatever it emits for the reconnect itself, so a test can
    inspect just that first message.
    """

    async def is_disconnected(self) -> bool:
        return True


class TestSSEReconnectGap:
    """A reconnect whose Last-Event-ID has fallen out of the ring."""

    @pytest.mark.asyncio
    async def test_gap_too_large_emits_resync_not_connected(self) -> None:
        """The client only invalidates its cache on a 'resync' event -- see
        ui/src/hooks/use-sse.ts. Emitting 'connected' here (what a fresh,
        first-ever connection sends) would leave a stale cache in place
        after everything that happened during the gap."""
        ring = EventRing(max_size=2)
        account_id = uuid.uuid4()
        for i in range(5):
            await ring.add(account_id, "mail.new", {"n": i})

        oldest_id_in_ring = ring._rings[str(account_id)][0]["id"]
        stale_last_event_id = oldest_id_in_ring - 1
        assert not ring.has_events_after(stale_last_event_id, str(account_id))

        messages = [
            chunk
            async for chunk in _sse_generator(
                ring,
                str(account_id),
                stale_last_event_id,
                _DisconnectedRequest(),
            )
        ]

        assert len(messages) == 1
        assert "event: resync" in messages[0]
        assert "event: connected" not in messages[0]

    @pytest.mark.asyncio
    async def test_replayable_gap_replays_missed_events(self) -> None:
        """A Last-Event-ID still inside the ring replays, and never resyncs."""
        ring = EventRing(max_size=10)
        account_id = uuid.uuid4()
        first_id = await ring.add(account_id, "mail.new", {"n": 0})
        await ring.add(account_id, "mail.new", {"n": 1})

        messages = [
            chunk
            async for chunk in _sse_generator(
                ring,
                str(account_id),
                first_id,
                _DisconnectedRequest(),
            )
        ]

        assert len(messages) == 1
        assert "event: mail.new" in messages[0]
        assert "event: resync" not in messages[0]


class _StubRequest:
    """Just enough of a Starlette Request for sse_endpoint, disconnected on
    the first poll."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers
        self.query_params: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        return True


class TestSSEEventIdsNameTheirRing:
    """Sequence ids restart with every process, so a browser reconnecting
    after a restart presents an id the new process never issued -- usually
    higher than anything it has handed out yet. That must be answered with a
    resync: replaying from it delivers nothing, and resuming from it skips
    every new event until the counter catches up."""

    @pytest.mark.asyncio
    async def test_reconnect_with_an_id_past_the_ring_resyncs(self) -> None:
        ring = EventRing()  # a freshly started process, nothing emitted yet

        messages = [
            chunk async for chunk in _sse_generator(ring, None, 5000, _DisconnectedRequest())
        ]

        assert messages, "the reconnect was answered with nothing at all"
        assert "event: resync" in messages[0]

    @pytest.mark.asyncio
    async def test_endpoint_resyncs_a_client_presenting_another_rings_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        previous_process = EventRing()
        ring = EventRing()
        account_id = uuid.uuid4()
        await ring.add(account_id, "mail.new", {"n": 1})
        await ring.add(account_id, "mail.new", {"n": 2})
        monkeypatch.setattr(events_module, "_event_ring", ring)

        request = _StubRequest({"Last-Event-ID": previous_process.format_event_id(1)})
        response = await events_module.sse_endpoint(request)  # type: ignore[arg-type]
        chunks = [chunk async for chunk in response.body_iterator]  # type: ignore[attr-defined]

        assert chunks and "event: resync" in chunks[0], chunks

    @pytest.mark.asyncio
    async def test_every_id_handed_out_names_this_ring(self) -> None:
        ring = EventRing()
        account_id = uuid.uuid4()
        first_id = await ring.add(account_id, "mail.new", {"n": 0})
        await ring.add(account_id, "mail.new", {"n": 1})

        fresh = [chunk async for chunk in _sse_generator(ring, None, None, _DisconnectedRequest())]
        replayed = [
            chunk
            async for chunk in _sse_generator(
                ring, str(account_id), first_id, _DisconnectedRequest()
            )
        ]

        for chunk in (*fresh, *replayed):
            assert chunk.startswith(f"id: {ring.epoch}-"), chunk

    def test_an_id_round_trips_only_through_the_ring_that_issued_it(self) -> None:
        ring = EventRing()
        other = EventRing()
        assert ring.parse_event_id(ring.format_event_id(7)) == 7
        assert ring.parse_event_id(other.format_event_id(7)) is None
        assert ring.parse_event_id("7") is None
        assert ring.parse_event_id("not-an-id") is None
