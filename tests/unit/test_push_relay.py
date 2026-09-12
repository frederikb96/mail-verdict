"""push/relay.py against a fake relay: how each answer maps to what happens to
the subscription row, what the request carries, and the read-sync throttle."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest

from mail_verdict.config.loader import PushConfig
from mail_verdict.database.models import PushSubscription
from mail_verdict.push.relay import RelayClient, RelayOutcome

_KEY = "00" * 32
_RELAY = "https://relay.example"


def _config(**overrides: Any) -> PushConfig:
    values: dict[str, Any] = {
        "apns_relay_urls": [_RELAY], "read_sync_min_interval_seconds": 600.0,
        "relay_timeout_seconds": 5.0,
    }
    values.update(overrides)
    return PushConfig(**values)


def _client(handler: Any, **kwargs: Any) -> RelayClient:
    return RelayClient(
        encryption_key=_KEY, config=_config(), transport=httpx.MockTransport(handler), **kwargs,
    )


def _native_row(client: RelayClient, relay_url: str = _RELAY) -> PushSubscription:
    ticket, key = client.seal_credentials("the-ticket", bytes(32))
    return PushSubscription(
        id=uuid.uuid4(), transport="apns", installation_id=uuid.uuid4(), relay_url=relay_url,
        encrypted_relay_ticket=ticket, encrypted_content_key=key, muted_channels=[],
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (202, RelayOutcome.DELIVERED),
        (401, RelayOutcome.GONE),
        (410, RelayOutcome.GONE),
        (429, RelayOutcome.FAILED),
        (502, RelayOutcome.FAILED),
        (503, RelayOutcome.FAILED),
        (400, RelayOutcome.FAILED),
        (413, RelayOutcome.FAILED),
    ],
)
@pytest.mark.asyncio
async def test_relay_answers_map_to_outcomes(status: int, expected: RelayOutcome) -> None:
    client = _client(lambda request: httpx.Response(status, json={}))
    row = _native_row(client)
    outcome = await client.send_alert(
        row, ticket="t", blob="b", collapse_id="c", ttl_seconds=300,
    )
    assert outcome is expected


@pytest.mark.asyncio
async def test_an_unreachable_relay_is_a_failure_not_a_gone_device() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)
    outcome = await client.send_background(_native_row(client))
    assert outcome is RelayOutcome.FAILED


@pytest.mark.asyncio
async def test_an_alert_request_carries_the_ticket_and_blob_and_nothing_readable() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"apns_id": "x"})

    client = _client(handler)
    row = _native_row(client)
    ticket, _key = client.open_credentials(row)
    await client.send_alert(row, ticket=ticket, blob="c2VhbGVk", collapse_id="id", ttl_seconds=300)

    assert str(seen[0].url) == f"{_RELAY}/v1/push"
    body = json.loads(seen[0].content)
    assert body == {
        "ticket": "the-ticket", "type": "alert", "blob": "c2VhbGVk", "collapse_id": "id",
        "ttl_seconds": 300,
    }


@pytest.mark.asyncio
async def test_a_background_push_carries_no_blob() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(202, json={})

    client = _client(handler)
    await client.send_background(_native_row(client))
    assert seen[0]["type"] == "background"
    assert "blob" not in seen[0]


def test_credentials_under_another_encryption_key_do_not_open() -> None:
    from mail_verdict.core.encryption import EncryptionError

    writer = _client(lambda r: httpx.Response(202))
    row = _native_row(writer)
    reader = RelayClient(encryption_key="11" * 32, config=_config())
    with pytest.raises(EncryptionError):
        reader.open_credentials(row)


class TestAvailability:
    def test_no_encryption_key_makes_native_push_unavailable(self) -> None:
        client = RelayClient(encryption_key="", config=_config())
        assert client.unavailable_reason() is not None
        assert not client.allows(_RELAY)

    def test_an_empty_relay_list_disables_native_push(self) -> None:
        client = RelayClient(encryption_key=_KEY, config=_config(apns_relay_urls=[]))
        assert client.unavailable_reason() is not None
        assert not client.allows(_RELAY)

    def test_a_relay_not_in_the_list_is_never_allowed(self) -> None:
        client = RelayClient(encryption_key=_KEY, config=_config())
        assert client.allows(_RELAY)
        assert not client.allows("https://other-relay.example")


@pytest.mark.asyncio
async def test_announcing_dismissed_alerts_schedules_a_read_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every resolve and dismiss path ends in announce_alerts_dismissed; a
    phone has no open stream, so this is its only way to hear of it."""
    from mail_verdict.alerts.resolve import announce_alerts_dismissed

    scheduled: list[object] = []

    class _Recorder:
        def schedule_read_sync(self, db: object) -> None:
            scheduled.append(db)

    monkeypatch.setattr("mail_verdict.alerts.resolve.get_relay_client_if_ready", _Recorder)
    marker = object()
    await announce_alerts_dismissed(marker, None)  # type: ignore[arg-type]
    assert scheduled == [marker]


class _FakeTime:
    """A clock the test moves by hand, and a sleep that waits for it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self._moved = asyncio.Event()

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        deadline = self.now + seconds
        while self.now < deadline:
            self._moved.clear()
            await self._moved.wait()

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self._moved.set()


class TestReadSyncThrottle:
    @pytest.fixture()
    def fake_time(self) -> _FakeTime:
        return _FakeTime()

    @pytest.fixture()
    def recorded(
        self, fake_time: _FakeTime, monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[RelayClient, list[float]]:
        client = RelayClient(
            encryption_key=_KEY, config=_config(read_sync_min_interval_seconds=600.0),
            clock=fake_time.clock, sleep=fake_time.sleep,
        )
        sends: list[float] = []

        async def record(db: object) -> None:
            sends.append(fake_time.now)

        monkeypatch.setattr(client, "_read_sync", record)
        return client, sends

    @staticmethod
    async def _settle() -> None:
        for _ in range(5):
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_one_send_per_window_with_a_trailing_send_for_later_changes(
        self, fake_time: _FakeTime, recorded: tuple[RelayClient, list[float]],
    ) -> None:
        client, sends = recorded

        client.schedule_read_sync(db=None)  # type: ignore[arg-type]
        await self._settle()
        assert sends == [1000.0]

        # Three more changes inside the window fold into one trailing send.
        for _ in range(3):
            fake_time.advance(100)
            client.schedule_read_sync(db=None)  # type: ignore[arg-type]
            await self._settle()
        assert sends == [1000.0]

        fake_time.advance(300)  # the window ends at 1600
        await self._settle()
        assert sends == [1000.0, 1600.0]

        # A change right after the trailing send waits out a full window again.
        fake_time.advance(10)
        client.schedule_read_sync(db=None)  # type: ignore[arg-type]
        await self._settle()
        assert sends == [1000.0, 1600.0]
        fake_time.advance(590)
        await self._settle()
        assert sends == [1000.0, 1600.0, 2200.0]

    @pytest.mark.asyncio
    async def test_a_change_after_a_quiet_window_is_sent_at_once(
        self, fake_time: _FakeTime, recorded: tuple[RelayClient, list[float]],
    ) -> None:
        client, sends = recorded
        client.schedule_read_sync(db=None)  # type: ignore[arg-type]
        await self._settle()
        fake_time.advance(601)
        client.schedule_read_sync(db=None)  # type: ignore[arg-type]
        await self._settle()
        assert sends == [1000.0, 1601.0]

    @pytest.mark.asyncio
    async def test_nothing_is_scheduled_while_native_push_is_unavailable(
        self, fake_time: _FakeTime, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = RelayClient(
            encryption_key=_KEY, config=_config(apns_relay_urls=[]),
            clock=fake_time.clock, sleep=fake_time.sleep,
        )
        sends: list[float] = []

        async def record(db: object) -> None:
            sends.append(fake_time.now)

        monkeypatch.setattr(client, "_read_sync", record)
        client.schedule_read_sync(db=None)  # type: ignore[arg-type]
        await self._settle()
        assert sends == []
