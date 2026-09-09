"""
account_prefs.trash_retention_days round-trips through the accounts API
-- proof that it is routed as an AccountPrefs field (update_account's
own prefs_fields set), not forwarded to PostIMAP's accounts table, where
it has no column and the write would fail.
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

from mail_verdict.api.accounts import router as accounts_router
from mail_verdict.database.connection import DatabaseConnection

_ACCOUNTS_DB_TARGET = "mail_verdict.api.accounts.get_db_connection"
_ACCOUNTS_EVENT_RING_TARGET = "mail_verdict.api.accounts.get_event_ring"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(accounts_router)
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


async def _seed(migrated_db: DatabaseConnection) -> uuid.UUID:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        await session.commit()
    return account_id


class TestTrashRetentionDaysRoundTrip:
    def test_defaults_to_off(self, client: TestClient, migrated_db: DatabaseConnection) -> None:
        account_id = client.portal.call(_seed, migrated_db)
        with (
            patch(_ACCOUNTS_DB_TARGET, return_value=migrated_db),
            patch(_ACCOUNTS_EVENT_RING_TARGET, return_value=None),
        ):
            resp = client.get(f"/accounts/{account_id}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["trash_retention_days"] is None

    def test_patch_sets_it_and_get_reflects_it(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = client.portal.call(_seed, migrated_db)
        with (
            patch(_ACCOUNTS_DB_TARGET, return_value=migrated_db),
            patch(_ACCOUNTS_EVENT_RING_TARGET, return_value=None),
        ):
            patch_resp = client.patch(
                f"/accounts/{account_id}", json={"trash_retention_days": 30},
            )
            assert patch_resp.status_code == 200, patch_resp.text
            assert patch_resp.json()["trash_retention_days"] == 30

            get_resp = client.get(f"/accounts/{account_id}")
        assert get_resp.json()["trash_retention_days"] == 30

    def test_patch_with_null_clears_it(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = client.portal.call(_seed, migrated_db)
        with (
            patch(_ACCOUNTS_DB_TARGET, return_value=migrated_db),
            patch(_ACCOUNTS_EVENT_RING_TARGET, return_value=None),
        ):
            client.patch(f"/accounts/{account_id}", json={"trash_retention_days": 30})
            resp = client.patch(f"/accounts/{account_id}", json={"trash_retention_days": None})
        assert resp.status_code == 200, resp.text
        assert resp.json()["trash_retention_days"] is None

    def test_create_account_can_set_it_up_front(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        with (
            patch(_ACCOUNTS_DB_TARGET, return_value=migrated_db),
            patch(_ACCOUNTS_EVENT_RING_TARGET, return_value=None),
        ):
            resp = client.post(
                "/accounts",
                json={
                    "name": "with-retention", "imap_host": "imap.example.com",
                    "imap_user": "user@example.com", "trash_retention_days": 14,
                },
            )
        assert resp.status_code == 201, resp.text
        assert resp.json()["trash_retention_days"] == 14
