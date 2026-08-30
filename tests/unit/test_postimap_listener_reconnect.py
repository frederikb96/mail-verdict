"""The postimap_events listener must reconnect once its connection closes.

asyncpg marks a connection closed without raising -- a Postgres restart, a
failover, an idle-connection reaper, a network blip. Skipping the keepalive
probe on a closed connection (instead of treating that as the reconnect
trigger) leaves the loop spinning on a dead connection forever, with
nothing logged after the initial warning. A failed reconnect attempt must
not give up after a single try either, or the listener wedges the same way.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mail_verdict.postimap.listener import CHANNEL, RECONNECT_DELAY_S, PostimapListener


@pytest.fixture()
def listener() -> PostimapListener:
    return PostimapListener("postgresql://user:pass@host/db")


@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_keepalive_reconnects_on_a_closed_connection(
    listener: PostimapListener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection asyncpg already marked closed triggers a reconnect, not a skip.

    Bounded to 5s: a regression back to skipping the probe never calls
    _reconnect, so nothing ever sets _running False and the loop spins
    forever with sleep mocked out -- this turns that hang into a clean
    failure instead of stalling the suite.
    """
    closed_conn = MagicMock()
    closed_conn.is_closed.return_value = True
    listener._conn = closed_conn
    listener._running = True

    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))

    async def _stop_after_one_reconnect() -> None:
        listener._running = False

    reconnect_mock = AsyncMock(side_effect=_stop_after_one_reconnect)
    with patch.object(listener, "_reconnect", reconnect_mock):
        await listener._keepalive()

    reconnect_mock.assert_awaited_once()
    closed_conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_keepalive_probes_an_open_connection_without_reconnecting(
    listener: PostimapListener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: a genuinely open connection is probed, not reconnected."""
    open_conn = MagicMock()
    open_conn.is_closed.return_value = False
    open_conn.execute = AsyncMock(return_value=None)
    listener._conn = open_conn
    listener._running = True

    async def _fake_sleep(_delay: float) -> None:
        listener._running = False

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    reconnect_mock = AsyncMock()
    with patch.object(listener, "_reconnect", reconnect_mock):
        await listener._keepalive()

    open_conn.execute.assert_awaited_once_with("SELECT 1")
    reconnect_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconnect_retries_with_backoff_until_it_succeeds(
    listener: PostimapListener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed reconnect attempt must not give up after one try."""
    listener._running = True
    stale_conn = AsyncMock()
    listener._conn = stale_conn

    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    new_conn = AsyncMock()
    connect_mock = AsyncMock(
        side_effect=[
            ConnectionError("first attempt fails"),
            ConnectionError("second attempt fails"),
            new_conn,
        ]
    )
    with patch("mail_verdict.postimap.listener.asyncpg.connect", connect_mock):
        await listener._reconnect()

    assert connect_mock.await_count == 3
    assert listener._conn is new_conn
    new_conn.add_listener.assert_awaited_once_with(CHANNEL, listener._on_notify)
    stale_conn.close.assert_awaited_once()
    assert sleeps == [RECONNECT_DELAY_S, RECONNECT_DELAY_S * 2]
