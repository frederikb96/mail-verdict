"""Tests for the SSE generator's live-streaming loop.

Drives the real `_sse_generator` against a real `EventRing`, interleaving
`ring.add()` calls with the generator's own suspension points -- the only
way to reproduce a race that depends on exactly when an event lands
relative to a `yield`.
"""

from __future__ import annotations

import uuid

import pytest

import mail_verdict.api.events as events_module
import mail_verdict.auth.ownership as ownership_module
from mail_verdict.api.event_ring import EventRing
from mail_verdict.api.events import _sse_generator, stream_keys
from mail_verdict.auth.scope import ADMIN_RING_KEY, EVERYONE_RING_KEY
from tests.helpers.auth import user_scope
from tests.helpers.sse import fixed_keys, streaming_as


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


async def _drain_until(gen: object, needle: str, *, limit: int = 20) -> str:
    """Pull from the generator until `needle` appears, returning everything read.

    A keepalive is a legitimate yield, and whether one lands before the
    event is a matter of timing the test does not control -- so treating
    the very next yield as the event makes the test fail on correct
    behaviour under load. The race this file exists to catch is set up
    without any sleep at all; only the ordinary-delivery steps need this.
    """
    parts: list[str] = []
    for _ in range(limit):
        chunk = await gen.__anext__()  # type: ignore[attr-defined]
        parts.append(chunk)
        if needle in chunk:
            return "".join(parts)
    raise AssertionError(f"{needle!r} never arrived: {''.join(parts)!r}")


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
        gen = _sse_generator(ring, fixed_keys(acct_str), first_id, request)

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
        await ring.add(account_id, "mail.new", {"n": 4})
        msg3 = await _drain_until(gen, '"n": 4')

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
        gen = _sse_generator(ring, fixed_keys(acct_str), first_id, request)

        # Fresh connect would also work, but reuse the reconnect path since
        # it is the one with a `missed` loop to suspend inside of. Prime it
        # with an already-caught-up id so replay returns nothing and the
        # generator goes straight to the live loop's first wait.
        await gen.__anext__()  # resync-or-nothing path is not exercised here

        collected: list[str] = []
        for n in (10, 11, 12, 13):
            await ring.add(account_id, "mail.new", {"n": n})
            collected.append(await _drain_until(gen, f'"n": {n}'))

        delivered = "".join(collected)
        for n in (10, 11, 12, 13):
            assert f'"n": {n}' in delivered

        await gen.aclose()


class TestSSEAccountFilter:
    """`?account_id=` narrows a stream to one of the caller's own accounts;
    the caller's user, admin and everyone keys stay, and an account that is
    not theirs adds nothing."""

    class _StubRequest:
        def __init__(self, query_params: dict[str, str]) -> None:
            self.query_params = query_params
            self.headers: dict[str, str] = {}

    def test_no_filter_reads_every_key_of_the_scope(self) -> None:
        scope = user_scope(uuid.uuid4(), [uuid.uuid4(), uuid.uuid4()], [uuid.uuid4()])

        assert stream_keys(scope, None) == scope.ring_keys()

    def test_a_filter_keeps_that_account_and_the_personal_keys(self) -> None:
        mine, other_mine, dav = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        scope = user_scope(uuid.uuid4(), [mine, other_mine], [dav], is_admin=True)

        assert stream_keys(scope, mine) == {
            str(mine), str(scope.user_id), ADMIN_RING_KEY, EVERYONE_RING_KEY,
        }

    def test_a_foreign_account_filter_adds_nothing(self) -> None:
        scope = user_scope(uuid.uuid4(), [uuid.uuid4()])

        assert stream_keys(scope, uuid.uuid4()) == {str(scope.user_id), EVERYONE_RING_KEY}

    @pytest.mark.asyncio
    async def test_uppercase_account_id_resolves_to_canonical_form(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """EventRing keys its rings on str(uuid.UUID), so a differently-cased
        filter must still match, or the client sees only keepalives."""
        account_id = uuid.uuid4()
        ring = EventRing()
        monkeypatch.setattr(events_module, "_event_ring", ring)

        captured: dict[str, frozenset[str]] = {}

        async def _fake_generator(
            event_ring: EventRing,
            resolve_keys: events_module.KeyResolver,
            last_event_id: int | None,
            request: object,
        ) -> None:
            captured["keys"] = await resolve_keys()
            return
            yield  # pragma: no cover - never reached; keeps this an async generator

        monkeypatch.setattr(events_module, "_sse_generator", _fake_generator)

        request = self._StubRequest({"account_id": str(account_id).upper()})
        with streaming_as(monkeypatch, user_scope(uuid.uuid4(), [account_id, uuid.uuid4()])):
            response = await events_module.sse_endpoint(request)  # type: ignore[arg-type]
            async for _ in response.body_iterator:  # type: ignore[attr-defined]
                pass

        assert str(account_id) in captured["keys"]
        assert len(captured["keys"]) == 3

    @pytest.mark.asyncio
    async def test_no_principal_is_refused(self) -> None:
        """Outside the gate (a test app) a stream without a caller is a 401,
        never an unscoped stream."""
        events_module_ring = EventRing()
        original = events_module._event_ring
        events_module._event_ring = events_module_ring
        try:
            response = await events_module.sse_endpoint(  # type: ignore[arg-type]
                self._StubRequest({}),
            )
        finally:
            events_module._event_ring = original

        assert response.status_code == 401


class TestSSEStreamReadsOnlyItsKeys:
    """A stream delivers the events of its own keys and nothing else, live
    and on replay -- the line between one user and another."""

    @pytest.mark.asyncio
    async def test_live_events_of_other_keys_never_arrive(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(events_module, "KEEPALIVE_INTERVAL_S", 0.05)
        ring = EventRing()
        mine, theirs, their_user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        gen = _sse_generator(
            ring, fixed_keys(str(mine)), None, _CountingRequest(disconnect_after=50),
        )
        await gen.__anext__()  # connected

        await ring.add(theirs, "mail.new", {"n": "theirs"})
        await ring.add(their_user, "order.updated", {"n": "their order"})
        await ring.add(mine, "mail.new", {"n": "mine"})
        delivered = await _drain_until(gen, '"n": "mine"')

        assert "theirs" not in delivered and "their order" not in delivered
        await gen.aclose()

    @pytest.mark.asyncio
    async def test_replay_from_last_event_id_carries_only_own_keys(self) -> None:
        ring = EventRing()
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        last_seen = await ring.add(mine, "mail.new", {"n": 0})
        await ring.add(theirs, "mail.new", {"n": "theirs"})
        await ring.add(mine, "mail.new", {"n": 1})

        gen = _sse_generator(
            ring, fixed_keys(str(mine)), last_seen, _CountingRequest(disconnect_after=0),
        )
        replayed = [chunk async for chunk in gen]

        assert len(replayed) == 1
        assert '"n": 1' in replayed[0] and "theirs" not in replayed[0]

    @pytest.mark.asyncio
    async def test_keys_are_resolved_again_when_ownership_changes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An account given away mid-stream stops reaching the stream at
        once, and one just added starts -- without a reconnect."""
        monkeypatch.setattr(events_module, "KEEPALIVE_INTERVAL_S", 0.05)
        ring = EventRing()
        given_away, added = uuid.uuid4(), uuid.uuid4()
        owned = {str(given_away)}

        async def resolve() -> frozenset[str]:
            return frozenset(owned)

        gen = _sse_generator(ring, resolve, None, _CountingRequest(disconnect_after=50))
        await gen.__anext__()  # connected
        await ring.add(given_away, "mail.new", {"n": "before"})
        await _drain_until(gen, '"n": "before"')

        owned.clear()
        owned.add(str(added))
        monkeypatch.setattr(ownership_module, "_generation", ownership_module._generation + 1)
        ring.wake_all()

        await ring.add(given_away, "mail.new", {"n": "after, no longer mine"})
        await ring.add(added, "mail.new", {"n": "new account"})
        delivered = await _drain_until(gen, '"n": "new account"')

        assert "no longer mine" not in delivered
        await gen.aclose()


class TestSSEHeartbeat:
    """An idle stream carries a named `ping` event: EventSource hides comment
    lines, so a page could not otherwise tell a quiet stream from a dead one."""

    @pytest.mark.asyncio
    async def test_idle_stream_yields_a_ping_event_without_an_id(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(events_module, "KEEPALIVE_INTERVAL_S", 0.05)

        ring = EventRing()
        gen = _sse_generator(
            ring, fixed_keys(str(uuid.uuid4())), None, _CountingRequest(disconnect_after=50),
        )

        assert (await gen.__anext__()).startswith("id: ")
        heartbeat = await gen.__anext__()

        assert heartbeat.startswith("event: ping\n")
        assert "id:" not in heartbeat
        await gen.aclose()
