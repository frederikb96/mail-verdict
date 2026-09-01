"""
Identity CRUD and outbox from_addr resolution, against a real database.

A single persistent TestClient portal mounts both routers -- identities
and outbox -- since the property under test is that a compose request
naming an identity_id actually reaches outbox.from_addr; testing the two
routers in isolation would leave that wiring unverified.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.identities import router as identities_router
from mail_verdict.api.outbox import router as outbox_router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Identity, Outbox


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """A single persistent TestClient portal for the whole test -- a new
    `with TestClient(...)` per call would each open its own event loop in
    its own thread, and the shared migrated_db's asyncpg connections
    would bounce between them and fail with 'attached to a different
    loop' the moment a second call touches the database."""
    app = FastAPI()
    app.include_router(identities_router)
    app.include_router(outbox_router)
    with TestClient(app) as c:
        yield c


async def _seed_account(session: AsyncSession, imap_user: str = "user@example.com") -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, :imap_user, "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}", "imap_user": imap_user},
    )
    return account_id


async def _seed_two_accounts(migrated_db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID]:
    async with migrated_db.session() as session:
        account_a = await _seed_account(session, "a@example.com")
        account_b = await _seed_account(session, "b@example.com")
        await session.commit()
    return account_a, account_b


async def _identity_row(migrated_db: DatabaseConnection, identity_id: uuid.UUID) -> Identity:
    async with migrated_db.session() as session:
        result = await session.execute(select(Identity).where(Identity.id == identity_id))
        return result.scalar_one()


async def _default_identity_id(
    migrated_db: DatabaseConnection, account_id: uuid.UUID,
) -> uuid.UUID | None:
    async with migrated_db.session() as session:
        return await session.scalar(
            select(Identity.id).where(
                Identity.account_id == account_id, Identity.is_default.is_(True),
            )
        )


async def _outbox_from_addr(migrated_db: DatabaseConnection, outbox_id: uuid.UUID) -> str | None:
    async with migrated_db.session() as session:
        return await session.scalar(select(Outbox.from_addr).where(Outbox.id == outbox_id))


_IDENTITIES_TARGET = "mail_verdict.api.identities.get_db_connection"
_OUTBOX_TARGET = "mail_verdict.api.outbox.get_db_connection"


class TestIdentityCreate:
    def test_first_identity_is_forced_default_even_if_not_requested(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, _ = client.portal.call(_seed_two_accounts, migrated_db)
        with patch(_IDENTITIES_TARGET, return_value=migrated_db):
            resp = client.post(
                "/identities",
                json={"account_id": str(account_id), "email": "work@example.com"},
            )
        assert resp.status_code == 201, resp.text
        assert resp.json()["is_default"] is True

    def test_second_identity_requesting_default_takes_over(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, _ = client.portal.call(_seed_two_accounts, migrated_db)
        with patch(_IDENTITIES_TARGET, return_value=migrated_db):
            first = client.post(
                "/identities",
                json={"account_id": str(account_id), "email": "first@example.com"},
            )
            second = client.post(
                "/identities",
                json={
                    "account_id": str(account_id), "email": "second@example.com",
                    "is_default": True,
                },
            )
        assert first.json()["is_default"] is True
        assert second.json()["is_default"] is True
        first_row = client.portal.call(_identity_row, migrated_db, uuid.UUID(first.json()["id"]))
        assert first_row.is_default is False

    def test_second_identity_not_requesting_default_stays_non_default(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, _ = client.portal.call(_seed_two_accounts, migrated_db)
        with patch(_IDENTITIES_TARGET, return_value=migrated_db):
            client.post(
                "/identities",
                json={"account_id": str(account_id), "email": "first@example.com"},
            )
            second = client.post(
                "/identities",
                json={"account_id": str(account_id), "email": "second@example.com"},
            )
        assert second.json()["is_default"] is False

    def test_duplicate_address_case_insensitive_is_rejected(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, _ = client.portal.call(_seed_two_accounts, migrated_db)
        with patch(_IDENTITIES_TARGET, return_value=migrated_db):
            client.post(
                "/identities",
                json={"account_id": str(account_id), "email": "Same@Example.com"},
            )
            dup = client.post(
                "/identities",
                json={"account_id": str(account_id), "email": "same@example.com"},
            )
        assert dup.status_code == 409

    def test_same_address_on_a_different_account_is_allowed(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_a, account_b = client.portal.call(_seed_two_accounts, migrated_db)
        with patch(_IDENTITIES_TARGET, return_value=migrated_db):
            first = client.post(
                "/identities",
                json={"account_id": str(account_a), "email": "shared@example.com"},
            )
            second = client.post(
                "/identities",
                json={"account_id": str(account_b), "email": "shared@example.com"},
            )
        assert first.status_code == 201
        assert second.status_code == 201

    def test_unknown_account_is_rejected(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        with patch(_IDENTITIES_TARGET, return_value=migrated_db):
            resp = client.post(
                "/identities",
                json={"account_id": str(uuid.uuid4()), "email": "nobody@example.com"},
            )
        assert resp.status_code == 404


class TestIdentityUpdate:
    def test_unsetting_the_only_default_is_refused(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, _ = client.portal.call(_seed_two_accounts, migrated_db)
        with patch(_IDENTITIES_TARGET, return_value=migrated_db):
            created = client.post(
                "/identities",
                json={"account_id": str(account_id), "email": "only@example.com"},
            )
            identity_id = created.json()["id"]
            resp = client.patch(f"/identities/{identity_id}", json={"is_default": False})
        assert resp.status_code == 400

    def test_setting_a_new_default_unsets_the_old_one(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, _ = client.portal.call(_seed_two_accounts, migrated_db)
        with patch(_IDENTITIES_TARGET, return_value=migrated_db):
            first = client.post(
                "/identities",
                json={"account_id": str(account_id), "email": "first@example.com"},
            )
            second = client.post(
                "/identities",
                json={"account_id": str(account_id), "email": "second@example.com"},
            )
            resp = client.patch(f"/identities/{second.json()['id']}", json={"is_default": True})
        assert resp.status_code == 200
        assert resp.json()["is_default"] is True
        first_row = client.portal.call(_identity_row, migrated_db, uuid.UUID(first.json()["id"]))
        assert first_row.is_default is False

    def test_renaming_to_an_existing_address_on_the_account_is_rejected(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, _ = client.portal.call(_seed_two_accounts, migrated_db)
        with patch(_IDENTITIES_TARGET, return_value=migrated_db):
            client.post(
                "/identities",
                json={"account_id": str(account_id), "email": "taken@example.com"},
            )
            second = client.post(
                "/identities",
                json={"account_id": str(account_id), "email": "movable@example.com"},
            )
            resp = client.patch(
                f"/identities/{second.json()['id']}", json={"email": "taken@example.com"},
            )
        assert resp.status_code == 409


class TestIdentityDelete:
    def test_deleting_the_default_promotes_the_next_oldest_survivor(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, _ = client.portal.call(_seed_two_accounts, migrated_db)
        with patch(_IDENTITIES_TARGET, return_value=migrated_db):
            first = client.post(
                "/identities",
                json={"account_id": str(account_id), "email": "first@example.com"},
            )
            second = client.post(
                "/identities",
                json={"account_id": str(account_id), "email": "second@example.com"},
            )
            resp = client.delete(f"/identities/{first.json()['id']}")
        assert resp.status_code == 204
        remaining = client.portal.call(_default_identity_id, migrated_db, account_id)
        assert remaining == uuid.UUID(second.json()["id"])

    def test_deleting_the_last_identity_of_an_account_is_allowed(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, _ = client.portal.call(_seed_two_accounts, migrated_db)
        with patch(_IDENTITIES_TARGET, return_value=migrated_db):
            only = client.post(
                "/identities",
                json={"account_id": str(account_id), "email": "only@example.com"},
            )
            resp = client.delete(f"/identities/{only.json()['id']}")
            listing = client.get("/identities", params={"account_id": str(account_id)})
        assert resp.status_code == 204
        assert listing.json() == []

    def test_unknown_identity_is_a_404(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        with patch(_IDENTITIES_TARGET, return_value=migrated_db):
            resp = client.delete(f"/identities/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestOutboxIdentityResolution:
    def test_send_with_no_identity_and_none_configured_falls_back_to_imap_user(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """An account that never adopts identities behaves exactly as
        before this table existed: from_addr stays NULL, and PostIMAP
        falls back to accounts.imap_user on its own."""
        account_id, _ = client.portal.call(_seed_two_accounts, migrated_db)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            resp = client.post(
                "/outbox",
                json={
                    "account_id": str(account_id), "kind": "draft",
                    "to": ["them@example.com"], "subject": "hi", "body_text": "hi",
                },
            )
        assert resp.status_code == 201, resp.text
        assert resp.json()["from_addr"] is None

    def test_send_with_no_identity_named_uses_the_account_default(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, _ = client.portal.call(_seed_two_accounts, migrated_db)
        with patch(_IDENTITIES_TARGET, return_value=migrated_db):
            client.post(
                "/identities",
                json={"account_id": str(account_id), "email": "default@example.com"},
            )
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            resp = client.post(
                "/outbox",
                json={
                    "account_id": str(account_id), "kind": "draft",
                    "to": ["them@example.com"], "subject": "hi", "body_text": "hi",
                },
            )
        assert resp.status_code == 201, resp.text
        assert resp.json()["from_addr"] == "default@example.com"

    def test_send_with_an_explicit_identity_overrides_the_default(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, _ = client.portal.call(_seed_two_accounts, migrated_db)
        with patch(_IDENTITIES_TARGET, return_value=migrated_db):
            client.post(
                "/identities",
                json={"account_id": str(account_id), "email": "default@example.com"},
            )
            alias = client.post(
                "/identities",
                json={"account_id": str(account_id), "email": "alias@example.com"},
            )
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            resp = client.post(
                "/outbox",
                json={
                    "account_id": str(account_id), "kind": "draft",
                    "to": ["them@example.com"], "subject": "hi", "body_text": "hi",
                    "identity_id": alias.json()["id"],
                },
            )
        assert resp.status_code == 201, resp.text
        assert resp.json()["from_addr"] == "alias@example.com"

    def test_send_with_an_identity_from_a_different_account_is_rejected(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_a, account_b = client.portal.call(_seed_two_accounts, migrated_db)
        with patch(_IDENTITIES_TARGET, return_value=migrated_db):
            foreign = client.post(
                "/identities",
                json={"account_id": str(account_b), "email": "b@example.com"},
            )
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            resp = client.post(
                "/outbox",
                json={
                    "account_id": str(account_a), "kind": "draft",
                    "to": ["them@example.com"], "subject": "hi", "body_text": "hi",
                    "identity_id": foreign.json()["id"],
                },
            )
        assert resp.status_code == 400

    def test_send_with_an_unknown_identity_is_a_404(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, _ = client.portal.call(_seed_two_accounts, migrated_db)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            resp = client.post(
                "/outbox",
                json={
                    "account_id": str(account_id), "kind": "draft",
                    "to": ["them@example.com"], "subject": "hi", "body_text": "hi",
                    "identity_id": str(uuid.uuid4()),
                },
            )
        assert resp.status_code == 404
