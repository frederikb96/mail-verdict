"""
Native push against a real schema: who a native (apns) device is sent to,
what reaches its relay, what happens to its row on each relay answer, the
per-device badge, and the endpoints a native app and the web client use.

The relay itself is an httpx.MockTransport recording every request -- these
tests prove this server's own logic, never a real relay or APNs. Every
device gets its own ticket, and assertions select requests by ticket:
migrated_db is shared across the session, so devices other tests left
behind are sent to as well.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from mail_verdict.alerts.badge import badge_count
from mail_verdict.api.alerts import router as alerts_router
from mail_verdict.api.notifications import all_accounts_router as all_notifications_router
from mail_verdict.config.loader import PushConfig
from mail_verdict.core.encryption import decrypt
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Alert, PushSubscription
from mail_verdict.database.repository import AlertRepository, PushSubscriptionRepository
from mail_verdict.push.envelope import open_payload
from mail_verdict.push.relay import RelayClient
from mail_verdict.push.send import dispatch_push_for_alert
from mail_verdict.push.vapid import VapidKeyRepository
from mail_verdict.settings.service import (
    SettingsService,
    init_settings_service,
    reset_settings_service,
)
from tests.pg.test_bulk_actions_and_outbox import _seed_account_two_folders
from tests.pg.test_sync_notifications import _seed_notification

_ENCRYPTION_KEY = "00" * 32
_RELAY = "https://relay.example"


class _FakeRelay:
    """Records every push and answers with a per-ticket status (202 by default)."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status_by_ticket: dict[str, int] = {}
        self.client: RelayClient

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        return httpx.Response(self.status_by_ticket.get(body["ticket"], 202), json={})

    def for_ticket(self, ticket: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["ticket"] == ticket]


def _relay_client(fake: _FakeRelay, encryption_key: str = _ENCRYPTION_KEY) -> RelayClient:
    return RelayClient(
        encryption_key=encryption_key,
        config=PushConfig(
            apns_relay_urls=[_RELAY], read_sync_min_interval_seconds=600.0,
            relay_timeout_seconds=5.0,
        ),
        transport=httpx.MockTransport(fake.handler),
    )


@pytest.fixture()
def fake_relay(monkeypatch: pytest.MonkeyPatch) -> _FakeRelay:
    fake = _FakeRelay()
    fake.client = _relay_client(fake)
    monkeypatch.setattr("mail_verdict.push.relay._relay_client", fake.client)
    return fake


@pytest_asyncio.fixture()
async def settings_service(migrated_db: DatabaseConnection) -> AsyncIterator[SettingsService]:
    service = await init_settings_service(migrated_db)
    try:
        yield service
    finally:
        reset_settings_service()


class _Device:
    def __init__(self, sub: PushSubscription, ticket: str, key: bytes) -> None:
        self.sub, self.ticket, self.key = sub, ticket, key

    def opened(self, request: dict[str, Any]) -> dict[str, Any]:
        assert self.sub.installation_id is not None
        return open_payload(request["blob"], self.key, self.sub.installation_id)


async def _register(
    db: DatabaseConnection, relay: RelayClient, *, muted: list[str] | None = None,
    relay_url: str = _RELAY,
) -> _Device:
    ticket, key = f"ticket-{uuid.uuid4()}", os.urandom(32)
    encrypted_ticket, encrypted_key = relay.seal_credentials(ticket, key)
    sub = await PushSubscriptionRepository(db).upsert_native(
        installation_id=uuid.uuid4(), relay_url=relay_url,
        encrypted_relay_ticket=encrypted_ticket, encrypted_content_key=encrypted_key,
        label=None, muted_channels=muted,
    )
    return _Device(sub, ticket, key)


async def _inbox_and_junk(db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with db.session() as session:
        ids = await _seed_account_two_folders(session)
        await session.commit()
    return ids


def _mail_alert(account_id: uuid.UUID, folder_id: uuid.UUID, title: str = "Hello") -> Alert:
    message_id = uuid.uuid4()
    return Alert(
        id=uuid.uuid4(), kind="mail", title=title, body="sender@example.com",
        url=f"/?message={message_id}", account_id=account_id, message_id=message_id,
        folder_id=folder_id,
    )


def _stalled_alert(account_id: uuid.UUID) -> Alert:
    return Alert(
        id=uuid.uuid4(), kind="outbox_stalled", title="Still sending", body="x",
        account_id=account_id,
    )


class TestWhoIsSentTo:
    @pytest.mark.asyncio
    async def test_a_muted_channel_leaves_a_device_out_whatever_its_transport(
        self, migrated_db: DatabaseConnection, fake_relay: _FakeRelay,
    ) -> None:
        account_id, inbox_id, _junk = await _inbox_and_junk(migrated_db)
        repo = PushSubscriptionRepository(migrated_db)
        browser = await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label=None,
        )
        muted_browser = await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label=None,
        )
        await repo.update_prefs(muted_browser.id, muted_channels=["mail"])
        phone = await _register(migrated_db, fake_relay.client)
        mail_muted = await _register(migrated_db, fake_relay.client, muted=["mail"])
        system_muted = await _register(migrated_db, fake_relay.client, muted=["system"])

        for_mail = {s.id for s in await repo.list_for_alert(kind="mail", folder_id=inbox_id)}
        for_system = {
            s.id for s in await repo.list_for_alert(kind="outbox_stalled", folder_id=None)
        }

        assert {browser.id, phone.sub.id, system_muted.sub.id} <= for_mail
        assert muted_browser.id not in for_mail
        assert mail_muted.sub.id not in for_mail
        assert {browser.id, muted_browser.id, phone.sub.id, mail_muted.sub.id} <= for_system
        assert system_muted.sub.id not in for_system
        assert account_id  # seeded for the folder's own account row

    @pytest.mark.asyncio
    async def test_a_phone_receives_the_alert_and_a_phone_that_muted_mail_does_not(
        self, migrated_db: DatabaseConnection, fake_relay: _FakeRelay,
        settings_service: SettingsService,
    ) -> None:
        account_id, inbox_id, _junk = await _inbox_and_junk(migrated_db)
        phone = await _register(migrated_db, fake_relay.client)
        muted = await _register(migrated_db, fake_relay.client, muted=["mail"])
        alert = _mail_alert(account_id, inbox_id, title="Grüße")

        vapid = VapidKeyRepository(migrated_db, _ENCRYPTION_KEY)
        await dispatch_push_for_alert(
            migrated_db, vapid, alert, folder_id=inbox_id, relay=fake_relay.client,
        )

        [request] = fake_relay.for_ticket(phone.ticket)
        assert request["type"] == "alert"
        assert request["collapse_id"] == str(alert.id)
        payload = phone.opened(request)
        assert payload["alert_id"] == str(alert.id)
        assert payload["kind"] == "mail"
        assert payload["title"] == "Grüße"
        assert payload["account_id"] == str(account_id)
        assert isinstance(payload["badge"], int)
        assert fake_relay.for_ticket(muted.ticket) == []

        row = await PushSubscriptionRepository(migrated_db).get(phone.sub.id)
        assert row is not None and row.last_seen_at is not None and row.failed_at is None

    @pytest.mark.asyncio
    async def test_a_phone_is_sent_to_even_when_browser_push_is_unavailable(
        self, migrated_db: DatabaseConnection, fake_relay: _FakeRelay,
        settings_service: SettingsService, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A VAPID key that cannot be made must stop browser rows only."""
        account_id, inbox_id, _junk = await _inbox_and_junk(migrated_db)
        await PushSubscriptionRepository(migrated_db).upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label=None,
        )
        phone = await _register(migrated_db, fake_relay.client)
        never_called = AsyncMock()
        monkeypatch.setattr("mail_verdict.push.send.webpush_async", never_called)

        no_vapid = VapidKeyRepository(migrated_db, "")
        await dispatch_push_for_alert(
            migrated_db, no_vapid, _mail_alert(account_id, inbox_id), folder_id=inbox_id,
            relay=fake_relay.client,
        )

        assert len(fake_relay.for_ticket(phone.ticket)) == 1
        never_called.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_relay_no_longer_allowed_is_never_sent_to(
        self, migrated_db: DatabaseConnection, fake_relay: _FakeRelay,
        settings_service: SettingsService,
    ) -> None:
        account_id, inbox_id, _junk = await _inbox_and_junk(migrated_db)
        stranded = await _register(
            migrated_db, fake_relay.client, relay_url="https://retired-relay.example",
        )

        await dispatch_push_for_alert(
            migrated_db, VapidKeyRepository(migrated_db, _ENCRYPTION_KEY),
            _mail_alert(account_id, inbox_id), folder_id=inbox_id, relay=fake_relay.client,
        )

        assert fake_relay.for_ticket(stranded.ticket) == []
        row = await PushSubscriptionRepository(migrated_db).get(stranded.sub.id)
        assert row is not None and row.failed_at is None


class TestRelayAnswers:
    @pytest.mark.asyncio
    async def test_a_gone_device_is_removed_and_a_rate_limited_one_is_kept_as_failed(
        self, migrated_db: DatabaseConnection, fake_relay: _FakeRelay,
        settings_service: SettingsService,
    ) -> None:
        account_id, inbox_id, _junk = await _inbox_and_junk(migrated_db)
        gone = await _register(migrated_db, fake_relay.client)
        expired = await _register(migrated_db, fake_relay.client)
        limited = await _register(migrated_db, fake_relay.client)
        fake_relay.status_by_ticket.update(
            {gone.ticket: 410, expired.ticket: 401, limited.ticket: 429},
        )

        await dispatch_push_for_alert(
            migrated_db, VapidKeyRepository(migrated_db, _ENCRYPTION_KEY),
            _mail_alert(account_id, inbox_id), folder_id=inbox_id, relay=fake_relay.client,
        )

        repo = PushSubscriptionRepository(migrated_db)
        assert await repo.get(gone.sub.id) is None
        assert await repo.get(expired.sub.id) is None
        kept = await repo.get(limited.sub.id)
        assert kept is not None and kept.failed_at is not None

    @pytest.mark.asyncio
    async def test_the_envelope_names_mail_alerts_resolved_elsewhere(
        self, migrated_db: DatabaseConnection, fake_relay: _FakeRelay,
        settings_service: SettingsService,
    ) -> None:
        account_id, inbox_id, _junk = await _inbox_and_junk(migrated_db)
        alerts = AlertRepository(migrated_db)
        read_elsewhere = await alerts.create_mail_alert(
            account_id=account_id, message_id=uuid.uuid4(), msg_key=f"k-{uuid.uuid4()}",
            title="read", body=None, folder_id=inbox_id,
        )
        assert read_elsewhere is not None
        await alerts.dismiss(read_elsewhere.id)
        phone = await _register(migrated_db, fake_relay.client)

        await dispatch_push_for_alert(
            migrated_db, VapidKeyRepository(migrated_db, _ENCRYPTION_KEY),
            _mail_alert(account_id, inbox_id), folder_id=inbox_id, relay=fake_relay.client,
        )

        [request] = fake_relay.for_ticket(phone.ticket)
        assert str(read_elsewhere.id) in phone.opened(request)["resolved"]


class TestReadSync:
    @pytest.mark.asyncio
    async def test_a_read_sync_wakes_every_phone_and_drops_a_gone_one(
        self, migrated_db: DatabaseConnection, fake_relay: _FakeRelay,
    ) -> None:
        phone = await _register(migrated_db, fake_relay.client)
        gone = await _register(migrated_db, fake_relay.client)
        retired = await _register(
            migrated_db, fake_relay.client, relay_url="https://retired-relay.example",
        )
        fake_relay.status_by_ticket[gone.ticket] = 410

        await fake_relay.client._read_sync(migrated_db)

        [wake] = fake_relay.for_ticket(phone.ticket)
        assert wake["type"] == "background" and "blob" not in wake
        assert fake_relay.for_ticket(retired.ticket) == []
        assert await PushSubscriptionRepository(migrated_db).get(gone.sub.id) is None


class TestBadge:
    @pytest.mark.asyncio
    async def test_a_phone_badge_counts_its_own_scope_and_every_write_failure(
        self, migrated_db: DatabaseConnection, fake_relay: _FakeRelay,
        settings_service: SettingsService,
    ) -> None:
        """Deltas, not totals: the shared database carries other tests' rows.
        A new-mail alert in the inbox counts for a phone that never narrowed
        its folders, one in Junk does not, a stuck send always does, and a
        write failure counts on an inactive account as on an active one."""
        account_id, inbox_id, junk_id = await _inbox_and_junk(migrated_db)
        phone = await _register(migrated_db, fake_relay.client)
        before = await badge_count(migrated_db, settings_service, subscription=phone.sub)

        alerts = AlertRepository(migrated_db)
        for folder_id in (inbox_id, junk_id):
            await alerts.create_mail_alert(
                account_id=account_id, message_id=uuid.uuid4(), msg_key=f"k-{uuid.uuid4()}",
                title="t", body=None, folder_id=folder_id,
            )
        await alerts.create_outbox_stalled_alert(
            account_id=account_id, dedupe_key=f"outbox-stalled:{uuid.uuid4()}", title="t",
            body="b",
        )
        async with migrated_db.session() as session:
            inactive = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, name, imap_host, imap_port, imap_user, imap_password, is_active) "
                    "VALUES (:id, :name, 'imap.example.com', 993, 'u@example.com', "
                    "'\\x00' || convert_to('pw', 'UTF8'), false)"
                ),
                {"id": inactive, "name": f"inactive-{inactive}"},
            )
            await _seed_notification(session, account_id=account_id)
            await _seed_notification(session, account_id=inactive)
            await session.commit()

        after = await badge_count(migrated_db, settings_service, subscription=phone.sub)
        assert after - before == 1 + 1 + 2

    @pytest.mark.asyncio
    async def test_with_new_mail_off_only_system_notifications_count(
        self, migrated_db: DatabaseConnection, settings_service: SettingsService,
    ) -> None:
        account_id, inbox_id, _junk = await _inbox_and_junk(migrated_db)
        await settings_service.update("mail", {"bell_badge_counts_new_mail": False})
        try:
            before = await badge_count(migrated_db, settings_service, folder_ids=[inbox_id])
            alerts = AlertRepository(migrated_db)
            await alerts.create_mail_alert(
                account_id=account_id, message_id=uuid.uuid4(), msg_key=f"k-{uuid.uuid4()}",
                title="t", body=None, folder_id=inbox_id,
            )
            await alerts.create_outbox_stalled_alert(
                account_id=account_id, dedupe_key=f"outbox-stalled:{uuid.uuid4()}", title="t",
                body="b",
            )
            after = await badge_count(migrated_db, settings_service, folder_ids=[inbox_id])
            assert after - before == 1
        finally:
            await settings_service.update("mail", {"bell_badge_counts_new_mail": True})


class TestRegistration:
    @pytest.mark.asyncio
    async def test_a_refresh_replaces_the_ticket_and_keeps_label_and_mutes(
        self, migrated_db: DatabaseConnection, fake_relay: _FakeRelay,
    ) -> None:
        repo = PushSubscriptionRepository(migrated_db)
        relay = fake_relay.client
        installation = uuid.uuid4()
        first_ticket, first_key = relay.seal_credentials("first", bytes(32))
        first = await repo.upsert_native(
            installation_id=installation, relay_url=_RELAY, encrypted_relay_ticket=first_ticket,
            encrypted_content_key=first_key, label="Phone", muted_channels=["system"],
        )
        await repo.mark_failed(first.id)

        second_ticket, second_key = relay.seal_credentials("second", bytes(32))
        second = await repo.upsert_native(
            installation_id=installation, relay_url=_RELAY, encrypted_relay_ticket=second_ticket,
            encrypted_content_key=second_key, label=None, muted_channels=None,
        )

        assert second.id == first.id
        assert second.label == "Phone"
        assert second.muted_channels == ["system"]
        assert second.failed_at is None
        assert relay.open_credentials(second)[0] == "second"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """One persistent portal for the whole test -- see test_identities_api_pg.py."""
    app = FastAPI()
    app.include_router(alerts_router)
    app.include_router(all_notifications_router)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def api_ready(
    client: TestClient, migrated_db: DatabaseConnection, fake_relay: _FakeRelay,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_FakeRelay]:
    """The globals a running server's lifespan sets up for these routes."""
    monkeypatch.setattr(
        "mail_verdict.push.vapid._vapid_repo", VapidKeyRepository(migrated_db, _ENCRYPTION_KEY),
    )
    client.portal.call(init_settings_service, migrated_db)
    try:
        yield fake_relay
    finally:
        reset_settings_service()


def _content_key() -> tuple[bytes, str]:
    key = os.urandom(32)
    return key, base64.b64encode(key).decode()


def _register_body(**overrides: Any) -> dict[str, Any]:
    _key, encoded = _content_key()
    body: dict[str, Any] = {
        "installation_id": str(uuid.uuid4()), "relay_url": _RELAY,
        "ticket": f"ticket-{uuid.uuid4()}", "content_key": encoded, "label": "Phone",
    }
    body.update(overrides)
    return body


class TestNativeEndpoints:
    def test_native_push_config_says_whether_and_through_which_relays(
        self, client: TestClient, api_ready: _FakeRelay, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        response = client.get("/alerts/native-push")
        assert response.json() == {"available": True, "relay_urls": [_RELAY], "reason": None}

        monkeypatch.setattr(
            "mail_verdict.push.relay._relay_client", _relay_client(api_ready, encryption_key=""),
        )
        off = client.get("/alerts/native-push").json()
        assert off["available"] is False
        assert "ENCRYPTION_KEY" in off["reason"]

    def test_registering_stores_the_ticket_encrypted_and_lists_the_device_as_native(
        self, client: TestClient, api_ready: _FakeRelay, migrated_db: DatabaseConnection,
    ) -> None:
        body = _register_body(muted_channels=["system"])
        response = client.post("/alerts/subscriptions/native", json=body)

        assert response.status_code == 201, response.text
        created = response.json()
        assert created["transport"] == "apns"
        assert created["muted_channels"] == ["system"]
        assert "ticket" not in created and "content_key" not in created

        async def _stored() -> bytes:
            async with migrated_db.session() as session:
                return bytes(
                    await session.scalar(
                        text(
                            "SELECT encrypted_relay_ticket FROM push_subscriptions WHERE id = :id"
                        ),
                        {"id": uuid.UUID(created["id"])},
                    )
                )

        stored = client.portal.call(_stored)
        assert body["ticket"].encode() not in stored
        assert decrypt(stored, _ENCRYPTION_KEY) == body["ticket"]

        listed = client.get("/alerts/subscriptions").json()
        assert any(s["id"] == created["id"] and s["transport"] == "apns" for s in listed)

    def test_registering_is_refused_for_a_foreign_relay_a_bad_key_or_no_encryption_key(
        self, client: TestClient, api_ready: _FakeRelay, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        foreign = client.post(
            "/alerts/subscriptions/native",
            json=_register_body(relay_url="https://someone-elses-relay.example"),
        )
        assert foreign.status_code == 400

        short_key = base64.b64encode(os.urandom(31)).decode()
        assert client.post(
            "/alerts/subscriptions/native", json=_register_body(content_key=short_key),
        ).status_code == 422
        assert client.post(
            "/alerts/subscriptions/native", json=_register_body(muted_channels=["calendar"]),
        ).status_code == 422

        monkeypatch.setattr(
            "mail_verdict.push.relay._relay_client", _relay_client(api_ready, encryption_key=""),
        )
        assert client.post(
            "/alerts/subscriptions/native", json=_register_body(),
        ).status_code == 503

    def test_a_test_push_reaches_the_device_and_reports_a_refusal(
        self, client: TestClient, api_ready: _FakeRelay,
    ) -> None:
        key, encoded = _content_key()
        body = _register_body(content_key=encoded)
        created = client.post("/alerts/subscriptions/native", json=body).json()

        assert client.post(f"/alerts/subscriptions/{created['id']}/test").status_code == 204
        [request] = api_ready.for_ticket(body["ticket"])
        payload = open_payload(request["blob"], key, uuid.UUID(body["installation_id"]))
        assert payload["title"] == "Notifications are working"
        assert payload["kind"] != "mail"

        api_ready.status_by_ticket[body["ticket"]] = 429
        assert client.post(f"/alerts/subscriptions/{created['id']}/test").status_code == 502
        api_ready.status_by_ticket[body["ticket"]] = 410
        assert client.post(f"/alerts/subscriptions/{created['id']}/test").status_code == 410
        assert client.post(f"/alerts/subscriptions/{created['id']}/test").status_code == 404

    def test_muting_through_patch_stops_that_channel(
        self, client: TestClient, api_ready: _FakeRelay, migrated_db: DatabaseConnection,
    ) -> None:
        created = client.post("/alerts/subscriptions/native", json=_register_body()).json()
        patched = client.patch(
            f"/alerts/subscriptions/{created['id']}", json={"muted_channels": ["mail"]},
        )
        assert patched.json()["muted_channels"] == ["mail"]

        async def _eligible() -> set[uuid.UUID]:
            repo = PushSubscriptionRepository(migrated_db)
            return {s.id for s in await repo.list_for_alert(kind="mail", folder_id=None)}

        assert uuid.UUID(created["id"]) not in client.portal.call(_eligible)

    def test_lookup_returns_only_alerts_that_still_exist(
        self, client: TestClient, api_ready: _FakeRelay, migrated_db: DatabaseConnection,
    ) -> None:
        async def _seed() -> uuid.UUID:
            alert = await AlertRepository(migrated_db).create_mail_alert(
                account_id=uuid.uuid4(), message_id=uuid.uuid4(), msg_key=f"k-{uuid.uuid4()}",
                title="t", body=None,
            )
            assert alert is not None
            return alert.id

        existing = client.portal.call(_seed)
        response = client.post(
            "/alerts/lookup", json={"ids": [str(existing), str(uuid.uuid4())]},
        )
        assert [a["id"] for a in response.json()] == [str(existing)]
        assert client.post(
            "/alerts/lookup", json={"ids": [str(uuid.uuid4()) for _ in range(201)]},
        ).status_code == 422

    def test_badge_for_a_device_and_for_an_unknown_one(
        self, client: TestClient, api_ready: _FakeRelay, migrated_db: DatabaseConnection,
    ) -> None:
        created = client.post("/alerts/subscriptions/native", json=_register_body()).json()

        async def _expected() -> int:
            sub = await PushSubscriptionRepository(migrated_db).get(uuid.UUID(created["id"]))
            assert sub is not None
            service = SettingsService(migrated_db)
            await service.load()
            return await badge_count(migrated_db, service, subscription=sub)

        response = client.get("/alerts/badge", params={"subscription_id": created["id"]})
        assert response.json() == {"count": client.portal.call(_expected)}
        assert client.get(
            "/alerts/badge", params={"subscription_id": str(uuid.uuid4())},
        ).status_code == 404


class TestAllAccountsNotifications:
    def test_lists_every_account_inactive_included_newest_first(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        async def _seed() -> tuple[int, int]:
            async with migrated_db.session() as session:
                ids = []
                for is_active in (True, False):
                    account_id = uuid.uuid4()
                    await session.execute(
                        text(
                            "INSERT INTO accounts "
                            "(id, name, imap_host, imap_port, imap_user, imap_password, "
                            "is_active) VALUES (:id, :name, 'imap.example.com', 993, "
                            "'u@example.com', '\\x00' || convert_to('pw', 'UTF8'), :active)"
                        ),
                        {"id": account_id, "name": f"acct-{account_id}", "active": is_active},
                    )
                    ids.append(await _seed_notification(session, account_id=account_id))
                await session.commit()
            return ids[0], ids[1]

        active_id, inactive_id = client.portal.call(_seed)
        rows = client.get("/notifications", params={"unacknowledged_only": True}).json()
        listed = [r["id"] for r in rows]

        assert active_id in listed and inactive_id in listed
        assert listed.index(inactive_id) < listed.index(active_id)
