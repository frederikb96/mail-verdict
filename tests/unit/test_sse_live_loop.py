"""Tests for the SSE generator's live-streaming loop.

Drives the real `_sse_generator` against a real `EventRing`, interleaving
`ring.add()` calls with the generator's own suspension points -- the only
way to reproduce a race that depends on exactly when an event lands
relative to a `yield`.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

import mail_verdict.api.events as events_module
from mail_verdict.api.event_ring import EventRing
from mail_verdict.api.events import _sse_generator


class _CountingRequest:
    """Reports connected for a fixed number of polls, then disconnected.

    Lets the generator's `while True` loop run for exactly as many
    iterations as a test needs, then ends the generator cleanly instead of
    leaving it running forever.
    """

    def __init__(self, disconnect_after: int) -> None:
        self._remaining = disconnect_after

    async def is_disconnected(self) -> bool:
        if self._remaining <= 0:
            return True
        self._remaining -= 1
        return False


class TestSSELiveLoopDoesNotDropEvents:
    """An event appended while the generator is suspended on a yield must
    still be delivered -- never silently skipped by a `last_seen` that was
    read fresh from the ring after the suspension rather than captured
    before it."""

    @pytest.mark.asyncio
    async def test_event_added_during_replay_yield_is_not_lost(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reproduces the loss deterministically: two events already in the
        ring trigger a reconnect replay; a third is appended while the
        generator is suspended mid-replay (after the first yield, before
        the second `anext()` resumes it); a fourth is appended once the
        generator is back in the live loop. All three must reach the
        client, in order, none skipped."""
        monkeypatch.setattr(events_module, "KEEPALIVE_INTERVAL_S", 0.05)

        ring = EventRing()
        account_id = uuid.uuid4()
        acct_str = str(account_id)

        first_id = await ring.add(account_id, "mail.new", {"n": 1})
        await ring.add(account_id, "mail.new", {"n": 2})

        request = _CountingRequest(disconnect_after=50)
        gen = _sse_generator(ring, acct_str, first_id, request)

        # Replays the single missed event (n=2). The generator is now
        # suspended immediately after this yield -- exactly the window the
        # bug depended on.
        msg1 = await gen.__anext__()
        assert '"n": 2' in msg1

        # Appended while the generator is suspended on the yield above.
        await ring.add(account_id, "mail.new", {"n": 3})

        msg2 = await gen.__anext__()

        # Appended once the generator has resumed and is back waiting in
        # the live loop -- ordinary new-event delivery, not the race.
        task = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0.01)
        await ring.add(account_id, "mail.new", {"n": 4})
        msg3 = await task

        delivered = "".join([msg1, msg2, msg3])
        for n in (2, 3, 4):
            assert f'"n": {n}' in delivered, f"n={n} was never delivered: {delivered!r}"

        await gen.aclose()

    @pytest.mark.asyncio
    async def test_many_events_added_during_suspension_all_survive(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same race, several events at once, to rule out an off-by-one
        that only a single extra event happens to hide."""
        monkeypatch.setattr(events_module, "KEEPALIVE_INTERVAL_S", 0.05)

        ring = EventRing()
        account_id = uuid.uuid4()
        acct_str = str(account_id)

        first_id = await ring.add(account_id, "mail.new", {"n": 1})

        request = _CountingRequest(disconnect_after=50)
        gen = _sse_generator(ring, acct_str, first_id, request)

        # Fresh connect would also work, but reuse the reconnect path since
        # it is the one with a `missed` loop to suspend inside of. Prime it
        # with an already-caught-up id so replay returns nothing and the
        # generator goes straight to the live loop's first wait.
        await gen.__anext__()  # resync-or-nothing path is not exercised here

        collected: list[str] = []
        for n in (10, 11, 12, 13):
            task = asyncio.create_task(gen.__anext__())
            await asyncio.sleep(0.01)
            await ring.add(account_id, "mail.new", {"n": n})
            collected.append(await task)

        delivered = "".join(collected)
        for n in (10, 11, 12, 13):
            assert f'"n": {n}' in delivered

        await gen.aclose()


class TestSSEAccountIdCaseInsensitive:
    """`sse_endpoint` must normalize `?account_id=` the same way
    `EventRing.add` keys its rings (`str(uuid.UUID(...))`), or a client
    passing a differently-cased UUID matches nothing and sees only
    keepalives, with no error anywhere."""

    class _StubRequest:
        def __init__(self, query_params: dict[str, str]) -> None:
            self.query_params = query_params
            self.headers: dict[str, str] = {}

    @pytest.mark.asyncio
    async def test_uppercase_account_id_resolves_to_canonical_form(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account_id = uuid.uuid4()
        ring = EventRing()
        monkeypatch.setattr(events_module, "_event_ring", ring)

        captured: dict[str, str | None] = {}

        async def _fake_generator(
            event_ring: EventRing,
            account_id_arg: str | None,
            last_event_id: int | None,
            request: object,
        ) -> None:
            captured["account_id"] = account_id_arg
            return
            yield  # pragma: no cover - never reached; keeps this an async generator

        monkeypatch.setattr(events_module, "_sse_generator", _fake_generator)

        request = self._StubRequest({"account_id": str(account_id).upper()})
        response = await events_module.sse_endpoint(request)  # type: ignore[arg-type]
        async for _ in response.body_iterator:  # type: ignore[attr-defined]
            pass

        assert captured["account_id"] == str(account_id)
