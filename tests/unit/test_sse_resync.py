"""Tests for the SSE generator's reconnect-gap handling."""

from __future__ import annotations

import uuid

import pytest

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
                ring, str(account_id), stale_last_event_id, _DisconnectedRequest(),
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
                ring, str(account_id), first_id, _DisconnectedRequest(),
            )
        ]

        assert len(messages) == 1
        assert "event: mail.new" in messages[0]
        assert "event: resync" not in messages[0]
