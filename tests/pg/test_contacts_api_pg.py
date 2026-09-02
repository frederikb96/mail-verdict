"""Contact API endpoints, against a real database and a real PostIMAP."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.contacts import router as contacts_router
from mail_verdict.database.connection import DatabaseConnection

_TARGET = "mail_verdict.api.contacts.get_db_connection"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(contacts_router)
    with TestClient(app) as c:
        yield c


async def _seed_addressbook(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    dav_account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dav_accounts (id, name, url, username, password) "
            "VALUES (:id, :name, 'https://dav.example.com/', 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": dav_account_id, "name": f"dav-{dav_account_id}"},
    )
    collection_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dav_collections (id, account_id, kind, slug, display_name) "
            "VALUES (:id, :account_id, 'addressbook', 'contacts', 'Contacts')"
        ),
        {"id": collection_id, "account_id": dav_account_id},
    )
    return dav_account_id, collection_id


async def _seed(db: DatabaseConnection) -> uuid.UUID:
    async with db.session() as session:
        _dav_account_id, collection_id = await _seed_addressbook(session)
        await session.commit()
    return collection_id


class TestCreateAndGet:
    def test_create_and_get_contact(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        addressbook_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/contacts",
                json={
                    "addressbook_id": str(addressbook_id), "summary": "Anna Mueller",
                    "emails": [{"email": "anna@example.com", "type": "work"}],
                    "organization": "Example GmbH",
                },
            )
            assert created.status_code == 201, created.text
            body = created.json()
            assert body["summary"] == "Anna Mueller"
            assert body["emails"][0]["email"] == "anna@example.com"
            assert body["organization"] == "Example GmbH"
            assert body["addressbook_name"] == "Contacts"

            fetched = client.get(f"/contacts/{body['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["summary"] == "Anna Mueller"

    def test_get_unknown_contact_is_404(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        with patch(_TARGET, return_value=migrated_db):
            resp = client.get(f"/contacts/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestListAndSearch:
    def test_list_is_paged(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        addressbook_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            for i in range(3):
                client.post(
                    "/contacts",
                    json={
                        "addressbook_id": str(addressbook_id), "summary": f"Contact {i}",
                        "emails": [{"email": f"c{i}@example.com"}],
                    },
                )
            # Scoped to this test's own address book -- the database is
            # shared across the whole pg session (see test_grant_boundary.py's
            # convention of never assuming a clean table), so an unscoped
            # listing would also see rows from every earlier test.
            first_page = client.get(
                "/contacts", params={"addressbook_id": str(addressbook_id), "limit": 2},
            )
        assert first_page.status_code == 200
        assert len(first_page.json()["contacts"]) == 2
        assert first_page.json()["has_more"] is True
        assert first_page.json()["next_cursor"] is not None

        with patch(_TARGET, return_value=migrated_db):
            second_page = client.get(
                "/contacts",
                params={
                    "addressbook_id": str(addressbook_id), "limit": 2,
                    "cursor": first_page.json()["next_cursor"],
                },
            )
        assert len(second_page.json()["contacts"]) == 1
        assert second_page.json()["has_more"] is False

    def test_search_returns_one_row_per_email(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """
        search_email_hits() filters on the parsed `summary`/`emails`
        columns, which PostIMAP writes back after a real outbound PUT
        against a live CalDAV/CardDAV server lands -- there is none here,
        so the row is seeded with those columns already populated, the
        same way test_grant_boundary.py seeds dav_objects directly as the
        database owner rather than through the restricted role. Proving
        PostIMAP itself fills them in from `data` is that project's own
        test suite's job, not this query's.
        """
        addressbook_id = client.portal.call(_seed, migrated_db)
        object_id = uuid.uuid4()

        async def _seed_synced_contact(db: DatabaseConnection) -> None:
            async with db.session() as session:
                dav_account_id = await session.scalar(
                    text("SELECT account_id FROM dav_collections WHERE id = :id"),
                    {"id": addressbook_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO dav_objects "
                        "(id, account_id, collection_id, kind, data, summary, emails) "
                        "VALUES (:id, :account_id, :collection_id, 'addressbook', "
                        "'BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Multi Email\r\n"
                        "EMAIL;TYPE=work:work@example.com\r\n"
                        "EMAIL;TYPE=home:home@example.com\r\nEND:VCARD\r\n', "
                        "'Multi Email', ARRAY['work@example.com', 'home@example.com'])"
                    ),
                    {
                        "id": object_id, "account_id": dav_account_id,
                        "collection_id": addressbook_id,
                    },
                )
                await session.commit()

        client.portal.call(_seed_synced_contact, migrated_db)

        with patch(_TARGET, return_value=migrated_db):
            results = client.get("/contacts/search", params={"q": "Multi"})
        assert results.status_code == 200
        assert {r["email"] for r in results.json()} == {"work@example.com", "home@example.com"}


class TestUpdateAndDelete:
    def test_update_replaces_email_list(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        addressbook_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/contacts",
                json={
                    "addressbook_id": str(addressbook_id), "summary": "Old Name",
                    "emails": [{"email": "old@example.com"}],
                },
            )
            contact_id = created.json()["id"]

            updated = client.patch(
                f"/contacts/{contact_id}",
                json={"summary": "New Name", "emails": [{"email": "new@example.com"}]},
            )
        assert updated.status_code == 200
        assert updated.json()["summary"] == "New Name"
        assert [e["email"] for e in updated.json()["emails"]] == ["new@example.com"]

    def test_delete_removes_the_contact(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        addressbook_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/contacts",
                json={
                    "addressbook_id": str(addressbook_id), "summary": "Delete me",
                    "emails": [{"email": "delete@example.com"}],
                },
            )
            contact_id = created.json()["id"]

            deleted = client.delete(f"/contacts/{contact_id}")
            assert deleted.status_code == 204

            gone = client.get(f"/contacts/{contact_id}")
        assert gone.status_code == 404
