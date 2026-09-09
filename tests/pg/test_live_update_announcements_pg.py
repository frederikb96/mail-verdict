"""
The sweep for self-announcement: every write path in this batch touches
a table MailVerdict owns, with nothing upstream to fire a NOTIFY on it --
so each is proven here to push the event another connected viewer's SSE
listener actually invalidates on, against a real database.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.account_order import router as account_order_router
from mail_verdict.api.accounts import router as accounts_router
from mail_verdict.api.event_ring import EventRing
from mail_verdict.api.folder_management import folder_prefs_router
from mail_verdict.api.folder_management import router as folder_order_router
from mail_verdict.api.settings_api import router as settings_router
from mail_verdict.api.unified import account_router as unified_account_router
from mail_verdict.api.unified import unified_router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.settings.credentials import (
    init_provider_credential_repo,
    reset_provider_credential_repo,
)
from mail_verdict.settings.service import init_settings_service, reset_settings_service


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(accounts_router)
    app.include_router(account_order_router)
    app.include_router(folder_order_router)
    app.include_router(folder_prefs_router)
    app.include_router(settings_router)
    app.include_router(unified_account_router)
    app.include_router(unified_router)
    with TestClient(app) as c:
        yield c


async def _seed_account(session: AsyncSession) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    return account_id


async def _seed_folder(session: AsyncSession, account_id: uuid.UUID) -> uuid.UUID:
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name) VALUES (:id, :account_id, 'INBOX')"
        ),
        {"id": folder_id, "account_id": account_id},
    )
    return folder_id


async def _seed_account_and_folder(migrated_db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID]:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        folder_id = await _seed_folder(session, account_id)
        await session.commit()
    return account_id, folder_id


_ACCOUNTS_TARGET = "mail_verdict.api.accounts.get_db_connection"
_ACCOUNTS_EVENT_RING_TARGET = "mail_verdict.api.accounts.get_event_ring"
_FOLDER_MGMT_TARGET = "mail_verdict.api.folder_management.get_db_connection"
_FOLDER_MGMT_EVENT_RING_TARGET = "mail_verdict.api.folder_management.get_event_ring"
_SETTINGS_EVENT_RING_TARGET = "mail_verdict.api.settings_api.get_event_ring"
_SETTINGS_DB_TARGET = "mail_verdict.api.settings_api.get_db_connection"
_UNIFIED_TARGET = "mail_verdict.api.unified.get_db_connection"
_UNIFIED_EVENT_RING_TARGET = "mail_verdict.api.unified.get_event_ring"
_ACCOUNT_ORDER_TARGET = "mail_verdict.api.account_order.get_db_connection"
_ACCOUNT_ORDER_EVENT_RING_TARGET = "mail_verdict.api.account_order.get_event_ring"


class TestAccountPrefsAnnouncesItself:
    """AccountPrefs (emoji, spam_enabled, folder_order) is MailVerdict's
    own table -- a patch touching only those writes nothing on the
    PostIMAP-owned accounts table, so no "account" NOTIFY fires."""

    def test_patching_only_emoji_announces_itself(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, _ = client.portal.call(_seed_account_and_folder, migrated_db)
        event_ring = EventRing()
        client.portal.call(event_ring.add, account_id, "test.seed", {})
        seq_before = event_ring.get_latest_seq()

        with (
            patch(_ACCOUNTS_TARGET, return_value=migrated_db),
            patch(_ACCOUNTS_EVENT_RING_TARGET, return_value=event_ring),
        ):
            resp = client.patch(f"/accounts/{account_id}", json={"emoji": "📬"})
        assert resp.status_code == 200, resp.text

        new_events = client.portal.call(event_ring.replay_from, seq_before, str(account_id))
        matching = [e for e in new_events if e["event_type"] == "account.changed"]
        assert len(matching) == 1, f"expected one account.changed event, got {new_events!r}"

    def test_setting_the_emoji_through_the_unified_endpoint_announces_itself(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, _ = client.portal.call(_seed_account_and_folder, migrated_db)
        event_ring = EventRing()
        client.portal.call(event_ring.add, account_id, "test.seed", {})
        seq_before = event_ring.get_latest_seq()

        with (
            patch(_UNIFIED_TARGET, return_value=migrated_db),
            patch(_UNIFIED_EVENT_RING_TARGET, return_value=event_ring),
        ):
            resp = client.put(f"/accounts/{account_id}/emoji", json={"emoji": "📭"})
        assert resp.status_code == 200, resp.text

        new_events = client.portal.call(event_ring.replay_from, seq_before, str(account_id))
        matching = [e for e in new_events if e["event_type"] == "account.changed"]
        assert len(matching) == 1, f"expected one account.changed event, got {new_events!r}"


class TestAccountOrderAnnouncesItself:
    """The account order is a global Setting row, not account-scoped --
    broadcast_event reaches every account's ring, this one included."""

    def test_saving_the_account_order_announces_itself(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, _ = client.portal.call(_seed_account_and_folder, migrated_db)
        event_ring = EventRing()
        client.portal.call(event_ring.add, account_id, "test.seed", {})
        seq_before = event_ring.get_latest_seq()

        with (
            patch(_ACCOUNT_ORDER_TARGET, return_value=migrated_db),
            patch(_ACCOUNT_ORDER_EVENT_RING_TARGET, return_value=event_ring),
        ):
            resp = client.put("/account-order", json={"order": [str(account_id)]})
        assert resp.status_code == 200, resp.text

        new_events = client.portal.call(event_ring.replay_from, seq_before, str(account_id))
        matching = [e for e in new_events if e["event_type"] == "account.changed"]
        assert len(matching) == 1, f"expected one account.changed event, got {new_events!r}"


class TestFolderPrefsAnnouncesItself:
    """FolderPrefs and AccountPrefs.folder_order are both MailVerdict's
    own, unlike real_time (idle_requested), which is a PostIMAP-owned
    column already covered by its own trigger."""

    def test_saving_folder_order_announces_itself(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, folder_id = client.portal.call(_seed_account_and_folder, migrated_db)
        event_ring = EventRing()
        client.portal.call(event_ring.add, account_id, "test.seed", {})
        seq_before = event_ring.get_latest_seq()

        with (
            patch(_FOLDER_MGMT_TARGET, return_value=migrated_db),
            patch(_FOLDER_MGMT_EVENT_RING_TARGET, return_value=event_ring),
        ):
            resp = client.put(
                f"/accounts/{account_id}/folder-order", json={"order": [str(folder_id)]},
            )
        assert resp.status_code == 200, resp.text

        new_events = client.portal.call(event_ring.replay_from, seq_before, str(account_id))
        matching = [e for e in new_events if e["event_type"] == "folder.changed"]
        assert len(matching) == 1, f"expected one folder.changed event, got {new_events!r}"

    def test_patching_only_visibility_announces_itself(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, folder_id = client.portal.call(_seed_account_and_folder, migrated_db)
        event_ring = EventRing()
        client.portal.call(event_ring.add, account_id, "test.seed", {})
        seq_before = event_ring.get_latest_seq()

        with (
            patch(_FOLDER_MGMT_TARGET, return_value=migrated_db),
            patch(_FOLDER_MGMT_EVENT_RING_TARGET, return_value=event_ring),
        ):
            resp = client.patch(f"/folders/{folder_id}/prefs", json={"is_visible": False})
        assert resp.status_code == 200, resp.text

        new_events = client.portal.call(event_ring.replay_from, seq_before, str(account_id))
        matching = [e for e in new_events if e["event_type"] == "folder.changed"]
        assert len(matching) == 1, f"expected one folder.changed event, got {new_events!r}"

    def test_saving_the_unified_folder_order_announces_itself(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        # The unified order is a global Setting row, not account-scoped --
        # broadcast_event reaches every account's ring, this one included.
        account_id, _ = client.portal.call(_seed_account_and_folder, migrated_db)
        event_ring = EventRing()
        client.portal.call(event_ring.add, account_id, "test.seed", {})
        seq_before = event_ring.get_latest_seq()

        with (
            patch(_UNIFIED_TARGET, return_value=migrated_db),
            patch(_UNIFIED_EVENT_RING_TARGET, return_value=event_ring),
        ):
            resp = client.put("/unified/folder-order", json={"order": ["Inbox"]})
        assert resp.status_code == 200, resp.text

        new_events = client.portal.call(event_ring.replay_from, seq_before, str(account_id))
        matching = [e for e in new_events if e["event_type"] == "folder.changed"]
        assert len(matching) == 1, f"expected one folder.changed event, got {new_events!r}"


class TestSettingsAnnouncesItself:
    """The settings table has no account scope at all -- see
    broadcast_event for why every account's ring gets a copy."""

    @pytest.fixture(autouse=True)
    def _reset_settings_after(self) -> Iterator[None]:
        """The settings service is a module-level global -- initialising
        it for this class's own writes must not leak into whatever test
        runs next in the same session."""
        yield
        reset_settings_service()
        reset_provider_credential_repo()

    def test_updating_a_category_announces_itself(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, _ = client.portal.call(_seed_account_and_folder, migrated_db)
        client.portal.call(init_settings_service, migrated_db)
        event_ring = EventRing()
        client.portal.call(event_ring.add, account_id, "test.seed", {})
        seq_before = event_ring.get_latest_seq()

        with (
            patch(_SETTINGS_DB_TARGET, return_value=migrated_db),
            patch(_SETTINGS_EVENT_RING_TARGET, return_value=event_ring),
        ):
            resp = client.put("/settings/retry", json={"data": {"max_retries": 6}})
        assert resp.status_code == 200, resp.text

        new_events = client.portal.call(event_ring.replay_from, seq_before, str(account_id))
        matching = [e for e in new_events if e["event_type"] == "settings.changed"]
        assert len(matching) == 1, f"expected one settings.changed event, got {new_events!r}"
        assert matching[0]["data"]["category"] == "retry"

    def test_bulk_import_announces_itself(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, _ = client.portal.call(_seed_account_and_folder, migrated_db)
        client.portal.call(init_settings_service, migrated_db)
        client.portal.call(init_provider_credential_repo, migrated_db, "")
        event_ring = EventRing()
        client.portal.call(event_ring.add, account_id, "test.seed", {})
        seq_before = event_ring.get_latest_seq()

        with (
            patch(_SETTINGS_DB_TARGET, return_value=migrated_db),
            patch(_SETTINGS_EVENT_RING_TARGET, return_value=event_ring),
        ):
            resp = client.post(
                "/settings/import", json={"data": {"retry": {"max_retries": 4}}},
            )
        assert resp.status_code == 200, resp.text

        new_events = client.portal.call(event_ring.replay_from, seq_before, str(account_id))
        matching = [e for e in new_events if e["event_type"] == "settings.changed"]
        assert len(matching) == 1, f"expected one settings.changed event, got {new_events!r}"
