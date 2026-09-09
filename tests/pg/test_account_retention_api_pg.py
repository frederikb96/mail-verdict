"""
account_prefs.trash_retention_days and .junk_retention_days round-trip
through the accounts API -- proof that both are routed as AccountPrefs
fields (update_account's own prefs_fields set), not forwarded to
PostIMAP's accounts table, where they have no column and the write
would fail -- and that the two periods are independently configurable,
not one setting applied to both folders.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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


class TestJunkRetentionDaysRoundTrip:
    """Same shape as trash_retention_days -- a second, independent field,
    not the same setting reused for a second folder."""

    def test_defaults_to_off(self, client: TestClient, migrated_db: DatabaseConnection) -> None:
        account_id = client.portal.call(_seed, migrated_db)
        with (
            patch(_ACCOUNTS_DB_TARGET, return_value=migrated_db),
            patch(_ACCOUNTS_EVENT_RING_TARGET, return_value=None),
        ):
            resp = client.get(f"/accounts/{account_id}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["junk_retention_days"] is None

    def test_patch_sets_it_and_get_reflects_it(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = client.portal.call(_seed, migrated_db)
        with (
            patch(_ACCOUNTS_DB_TARGET, return_value=migrated_db),
            patch(_ACCOUNTS_EVENT_RING_TARGET, return_value=None),
        ):
            patch_resp = client.patch(
                f"/accounts/{account_id}", json={"junk_retention_days": 7},
            )
            assert patch_resp.status_code == 200, patch_resp.text
            assert patch_resp.json()["junk_retention_days"] == 7

            get_resp = client.get(f"/accounts/{account_id}")
        assert get_resp.json()["junk_retention_days"] == 7

    def test_setting_one_period_leaves_the_other_untouched(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """The owner talked himself round to the same number (30) for
        both, but arrived there by considering a different number (7)
        for Junk first -- the two must never be coupled."""
        account_id = client.portal.call(_seed, migrated_db)
        with (
            patch(_ACCOUNTS_DB_TARGET, return_value=migrated_db),
            patch(_ACCOUNTS_EVENT_RING_TARGET, return_value=None),
        ):
            client.patch(f"/accounts/{account_id}", json={"trash_retention_days": 30})
            resp = client.patch(f"/accounts/{account_id}", json={"junk_retention_days": 7})

        assert resp.status_code == 200, resp.text
        assert resp.json()["trash_retention_days"] == 30
        assert resp.json()["junk_retention_days"] == 7


class TestRetentionDaysRejectsBelowOne:
    """Zero means every retention_entries row already stamped reads as
    overdue and a negative period puts the threshold in the future --
    either one clears Trash or Junk on the very next sweep tick. Neither
    reaches the sweep at all: the API schema rejects both before a
    request is even processed."""

    @pytest.mark.parametrize("field", ["trash_retention_days", "junk_retention_days"])
    @pytest.mark.parametrize("value", [0, -1, -30])
    def test_patch_rejects_zero_and_negative(
        self, client: TestClient, migrated_db: DatabaseConnection, field: str, value: int,
    ) -> None:
        account_id = client.portal.call(_seed, migrated_db)
        with (
            patch(_ACCOUNTS_DB_TARGET, return_value=migrated_db),
            patch(_ACCOUNTS_EVENT_RING_TARGET, return_value=None),
        ):
            resp = client.patch(f"/accounts/{account_id}", json={field: value})
            assert resp.status_code == 422, resp.text

            # Rejected before ever reaching the write -- the field stays
            # unset rather than landing at the invalid value.
            get_resp = client.get(f"/accounts/{account_id}")
        assert get_resp.json()[field] is None

    @pytest.mark.parametrize("field", ["trash_retention_days", "junk_retention_days"])
    def test_create_account_rejects_zero_up_front(
        self, client: TestClient, migrated_db: DatabaseConnection, field: str,
    ) -> None:
        with (
            patch(_ACCOUNTS_DB_TARGET, return_value=migrated_db),
            patch(_ACCOUNTS_EVENT_RING_TARGET, return_value=None),
        ):
            resp = client.post(
                "/accounts",
                json={
                    "name": f"rejects-{field}", "imap_host": "imap.example.com",
                    "imap_user": "user@example.com", field: 0,
                },
            )
        assert resp.status_code == 422, resp.text

    def test_one_day_is_the_smallest_accepted_value(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = client.portal.call(_seed, migrated_db)
        with (
            patch(_ACCOUNTS_DB_TARGET, return_value=migrated_db),
            patch(_ACCOUNTS_EVENT_RING_TARGET, return_value=None),
        ):
            resp = client.patch(f"/accounts/{account_id}", json={"trash_retention_days": 1})
        assert resp.status_code == 200, resp.text
        assert resp.json()["trash_retention_days"] == 1


class TestDatabaseFloorHoldsEvenBypassingTheApi:
    """The API schema's Field(ge=1) is not the only thing enforcing this
    -- a check constraint holds the same floor in the database, for a
    write the Pydantic schema never sees (a hand-written SQL statement
    against production, say, or a future MCP tool built directly on the
    repository layer)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("column", ["trash_retention_days", "junk_retention_days"])
    @pytest.mark.parametrize("value", [0, -1])
    async def test_a_raw_update_below_one_is_rejected(
        self, migrated_db: DatabaseConnection, column: str, value: int,
    ) -> None:
        account_id = await _seed(migrated_db)
        async with migrated_db.session() as session:
            await session.execute(
                text(
                    "INSERT INTO account_prefs (account_id) VALUES (:id) "
                    "ON CONFLICT (account_id) DO NOTHING"
                ),
                {"id": account_id},
            )
            await session.commit()

        async with migrated_db.session() as session:
            with pytest.raises(IntegrityError, match="ck_account_prefs_.*_retention_days_min"):
                await session.execute(
                    text(f"UPDATE account_prefs SET {column} = :value WHERE account_id = :id"),
                    {"value": value, "id": account_id},
                )
                await session.commit()
