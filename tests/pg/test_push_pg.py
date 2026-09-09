"""
push/vapid.py's key repository, database/repository.py's
PushSubscriptionRepository, and push/send.py's dispatch -- against a real
Postgres schema. webpush_async itself is monkeypatched throughout: these
tests prove this application's own logic (who gets sent to, what happens
on success/failure/gone), never a real push service.
"""

from __future__ import annotations

import base64
import uuid
from unittest.mock import AsyncMock

import pytest

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Alert
from mail_verdict.database.repository import PushSubscriptionRepository
from mail_verdict.push.send import dispatch_push_for_alert
from mail_verdict.push.vapid import VapidKeyRepository, VapidUnavailableError

_ENCRYPTION_KEY = "00" * 32
_OTHER_ENCRYPTION_KEY = "11" * 32


class TestVapidKeyRepository:
    @pytest.mark.asyncio
    async def test_no_encryption_key_raises(self, migrated_db: DatabaseConnection) -> None:
        repo = VapidKeyRepository(migrated_db, "")
        with pytest.raises(VapidUnavailableError):
            await repo.get_or_create()

    @pytest.mark.asyncio
    async def test_generates_once_and_persists(self, migrated_db: DatabaseConnection) -> None:
        repo = VapidKeyRepository(migrated_db, _ENCRYPTION_KEY)
        first = await repo.public_key_b64()

        # A second, independent repository instance (no shared in-memory
        # cache) must read back the same stored keypair rather than
        # generating a second one.
        second_repo = VapidKeyRepository(migrated_db, _ENCRYPTION_KEY)
        second = await second_repo.public_key_b64()
        assert first == second

    @pytest.mark.asyncio
    async def test_public_key_is_url_safe_base64_with_no_padding(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        repo = VapidKeyRepository(migrated_db, _ENCRYPTION_KEY)
        public_key = await repo.public_key_b64()
        assert "+" not in public_key
        assert "/" not in public_key
        assert "=" not in public_key
        # An uncompressed P-256 point is 65 bytes -- 0x04 plus two 32-byte
        # coordinates.
        padded = public_key + "=" * (-len(public_key) % 4)
        assert len(base64.urlsafe_b64decode(padded)) == 65

    @pytest.mark.asyncio
    async def test_wrong_key_on_read_raises(self, migrated_db: DatabaseConnection) -> None:
        writer = VapidKeyRepository(migrated_db, _ENCRYPTION_KEY)
        await writer.get_or_create()

        reader = VapidKeyRepository(migrated_db, _OTHER_ENCRYPTION_KEY)
        with pytest.raises(VapidUnavailableError):
            await reader.get_or_create()


class TestPushSubscriptionRepository:
    @pytest.mark.asyncio
    async def test_upsert_refreshes_an_existing_endpoint_rather_than_duplicating(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        repo = PushSubscriptionRepository(migrated_db)
        endpoint = f"https://push.example/{uuid.uuid4()}"
        first = await repo.upsert(endpoint=endpoint, p256dh="p1", auth="a1", label="First")
        second = await repo.upsert(endpoint=endpoint, p256dh="p2", auth="a2", label="Second")

        assert first.id == second.id
        rows = [s for s in await repo.list_all() if s.endpoint == endpoint]
        assert len(rows) == 1
        assert rows[0].p256dh == "p2"
        assert rows[0].auth == "a2"

    @pytest.mark.asyncio
    async def test_update_prefs_leaves_omitted_fields_untouched(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        repo = PushSubscriptionRepository(migrated_db)
        folder_id = uuid.uuid4()
        sub = await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label=None,
        )
        await repo.update_prefs(sub.id, alert_folder_ids=[folder_id])

        # Only reminders_enabled named this time -- alert_folder_ids must
        # survive untouched, not be nulled back to "every folder".
        updated = await repo.update_prefs(sub.id, reminders_enabled=False)
        assert updated is not None
        assert updated.alert_folder_ids == [folder_id]
        assert updated.reminders_enabled is False

    @pytest.mark.asyncio
    async def test_update_prefs_can_explicitly_reset_folder_ids_to_every_folder(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        repo = PushSubscriptionRepository(migrated_db)
        sub = await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label=None,
        )
        await repo.update_prefs(sub.id, alert_folder_ids=[uuid.uuid4()])
        updated = await repo.update_prefs(sub.id, alert_folder_ids=None)
        assert updated is not None
        assert updated.alert_folder_ids is None

    @pytest.mark.asyncio
    async def test_update_prefs_against_a_missing_id_returns_none(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        repo = PushSubscriptionRepository(migrated_db)
        assert await repo.update_prefs(uuid.uuid4(), reminders_enabled=False) is None

    @pytest.mark.asyncio
    async def test_delete_reports_whether_a_row_existed(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        repo = PushSubscriptionRepository(migrated_db)
        sub = await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label=None,
        )
        assert await repo.delete(sub.id) is True
        assert await repo.delete(sub.id) is False

    @pytest.mark.asyncio
    async def test_mark_seen_clears_a_prior_failure(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        repo = PushSubscriptionRepository(migrated_db)
        sub = await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label=None,
        )
        await repo.mark_failed(sub.id)
        failed = await repo.get(sub.id)
        assert failed is not None and failed.failed_at is not None

        await repo.mark_seen(sub.id)
        seen = await repo.get(sub.id)
        assert seen is not None and seen.failed_at is None and seen.last_seen_at is not None

    @pytest.mark.asyncio
    async def test_list_for_alert_mail_matches_null_scope_or_the_given_folder(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        repo = PushSubscriptionRepository(migrated_db)
        folder_a, folder_b = uuid.uuid4(), uuid.uuid4()
        every_folder = await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label="all",
        )
        scoped_a = await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label="a",
        )
        await repo.update_prefs(scoped_a.id, alert_folder_ids=[folder_a])
        scoped_b = await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label="b",
        )
        await repo.update_prefs(scoped_b.id, alert_folder_ids=[folder_b])

        matched = await repo.list_for_alert(kind="mail", folder_id=folder_a)
        matched_ids = {s.id for s in matched}
        assert every_folder.id in matched_ids
        assert scoped_a.id in matched_ids
        assert scoped_b.id not in matched_ids

    @pytest.mark.asyncio
    async def test_list_for_alert_mail_with_no_folder_only_matches_every_folder_scope(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        repo = PushSubscriptionRepository(migrated_db)
        every_folder = await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label="all",
        )
        scoped = await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label="s",
        )
        await repo.update_prefs(scoped.id, alert_folder_ids=[uuid.uuid4()])

        matched = await repo.list_for_alert(kind="mail", folder_id=None)
        matched_ids = {s.id for s in matched}
        assert every_folder.id in matched_ids
        assert scoped.id not in matched_ids

    @pytest.mark.asyncio
    async def test_list_for_alert_reminder_is_gated_on_reminders_enabled(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        repo = PushSubscriptionRepository(migrated_db)
        enabled = await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label="e",
        )
        disabled = await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label="d",
        )
        await repo.update_prefs(disabled.id, reminders_enabled=False)

        matched = await repo.list_for_alert(kind="reminder", folder_id=None)
        matched_ids = {s.id for s in matched}
        assert enabled.id in matched_ids
        assert disabled.id not in matched_ids


def _fake_alert(*, kind: str = "mail") -> Alert:
    return Alert(
        id=uuid.uuid4(), kind=kind, deliver_at=None, title="Hello", body="from someone",
        url="/?message=1", dedupe_key=f"mail:{uuid.uuid4()}",
    )


@pytest.fixture()
def vapid_repo(migrated_db: DatabaseConnection) -> VapidKeyRepository:
    return VapidKeyRepository(migrated_db, _ENCRYPTION_KEY)


class TestDispatchPushForAlert:
    @pytest.mark.asyncio
    async def test_sends_to_every_matching_subscription_and_marks_seen(
        self, migrated_db: DatabaseConnection, vapid_repo: VapidKeyRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = PushSubscriptionRepository(migrated_db)
        sub = await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label=None,
        )

        sent: list[str] = []

        async def fake_webpush(**kwargs: object) -> None:
            sent.append(kwargs["subscription_info"]["endpoint"])  # type: ignore[index]

        monkeypatch.setattr(
            "mail_verdict.push.send.webpush_async", AsyncMock(side_effect=fake_webpush),
        )

        await dispatch_push_for_alert(migrated_db, vapid_repo, _fake_alert(), folder_id=None)

        # Not asserted as the only send: the migrated_db fixture is shared
        # across this whole test file's session, and every "every folder"
        # subscription an earlier test left behind matches folder_id=None
        # too -- the same accumulation test_alerts_pg.py's own comments
        # already document. This test's own endpoint being among the
        # sends is what it actually proves.
        assert sub.endpoint in sent
        seen = await repo.get(sub.id)
        assert seen is not None and seen.last_seen_at is not None and seen.failed_at is None

    @pytest.mark.asyncio
    async def test_a_410_response_deletes_the_subscription(
        self, migrated_db: DatabaseConnection, vapid_repo: VapidKeyRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pywebpush import WebPushException

        repo = PushSubscriptionRepository(migrated_db)
        sub = await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label=None,
        )

        class _Resp:
            status_code = 410

        async def fake_webpush(**kwargs: object) -> None:
            raise WebPushException("gone", response=_Resp())

        monkeypatch.setattr(
            "mail_verdict.push.send.webpush_async", AsyncMock(side_effect=fake_webpush),
        )

        await dispatch_push_for_alert(migrated_db, vapid_repo, _fake_alert(), folder_id=None)

        assert await repo.get(sub.id) is None

    @pytest.mark.asyncio
    async def test_an_other_failure_stamps_failed_at_and_keeps_the_row(
        self, migrated_db: DatabaseConnection, vapid_repo: VapidKeyRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pywebpush import WebPushException

        repo = PushSubscriptionRepository(migrated_db)
        sub = await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label=None,
        )

        class _Resp:
            status_code = 503

        async def fake_webpush(**kwargs: object) -> None:
            raise WebPushException("unavailable", response=_Resp())

        monkeypatch.setattr(
            "mail_verdict.push.send.webpush_async", AsyncMock(side_effect=fake_webpush),
        )

        await dispatch_push_for_alert(migrated_db, vapid_repo, _fake_alert(), folder_id=None)

        row = await repo.get(sub.id)
        assert row is not None
        assert row.failed_at is not None

    @pytest.mark.asyncio
    async def test_no_encryption_key_is_a_silent_noop(
        self, migrated_db: DatabaseConnection, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = PushSubscriptionRepository(migrated_db)
        await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label=None,
        )
        never_called = AsyncMock()
        monkeypatch.setattr("mail_verdict.push.send.webpush_async", never_called)

        no_key_repo = VapidKeyRepository(migrated_db, "")
        await dispatch_push_for_alert(migrated_db, no_key_repo, _fake_alert(), folder_id=None)

        never_called.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_subscription_scoped_to_a_different_folder_is_skipped(
        self, migrated_db: DatabaseConnection, vapid_repo: VapidKeyRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Not asserted by counting calls: the migrated_db fixture is
        shared across this file's whole session, and an earlier test's
        "every folder" subscription would still be called for any
        folder_id. What is provable regardless of that accumulation is
        that THIS test's own folder-scoped subscription is excluded."""
        repo = PushSubscriptionRepository(migrated_db)
        other_folder = uuid.uuid4()
        sub = await repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label=None,
        )
        await repo.update_prefs(sub.id, alert_folder_ids=[other_folder])

        called_endpoints: list[str] = []

        async def fake_webpush(**kwargs: object) -> None:
            called_endpoints.append(kwargs["subscription_info"]["endpoint"])  # type: ignore[index]

        monkeypatch.setattr(
            "mail_verdict.push.send.webpush_async", AsyncMock(side_effect=fake_webpush),
        )

        target_folder = uuid.uuid4()
        await dispatch_push_for_alert(
            migrated_db, vapid_repo, _fake_alert(kind="mail"), folder_id=target_folder,
        )

        assert sub.endpoint not in called_endpoints
