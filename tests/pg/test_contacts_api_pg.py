"""Contact API endpoints, against a real database and a real PostIMAP."""

from __future__ import annotations

import asyncio
import base64
import os
import time
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.contacts import router as contacts_router
from mail_verdict.api.image_exceptions import router as image_exceptions_router
from mail_verdict.calendar.repository import CollectionRepository
from mail_verdict.database.connection import DatabaseConnection

_TARGET = "mail_verdict.api.contacts.get_db_connection"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(contacts_router)
    app.include_router(image_exceptions_router)
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


async def _seed_many_contacts(
    db: DatabaseConnection, addressbook_id: uuid.UUID, count: int, photo_payload: str,
) -> None:
    """`count` contacts in one address book, each carrying an embedded
    photo -- the shape a real synced address book has, not an
    adversarial one -- inserted in a single bulk statement rather than
    one round trip per row."""
    async with db.session() as session:
        dav_account_id = await session.scalar(
            text("SELECT account_id FROM dav_collections WHERE id = :id"),
            {"id": addressbook_id},
        )
        rows = [
            {
                "id": uuid.uuid4(),
                "account_id": dav_account_id,
                "collection_id": addressbook_id,
                "data": (
                    "BEGIN:VCARD\r\nVERSION:3.0\r\n"
                    f"FN:Contact {i}\r\n"
                    f"EMAIL:contact{i}@example.com\r\n"
                    f"PHOTO;ENCODING=b;TYPE=JPEG:{photo_payload}\r\n"
                    "END:VCARD\r\n"
                ),
                "summary": f"Contact {i}",
                "email": f"contact{i}@example.com",
            }
            for i in range(count)
        ]
        await session.execute(
            text(
                "INSERT INTO dav_objects "
                "(id, account_id, collection_id, kind, data, summary, emails) "
                "VALUES (:id, :account_id, :collection_id, 'addressbook', :data, "
                ":summary, ARRAY[:email])"
            ),
            rows,
        )
        await session.commit()


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

    def test_response_carries_the_full_field_set(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        addressbook_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/contacts",
                json={
                    "addressbook_id": str(addressbook_id), "summary": "Full Fields",
                    "emails": [{"email": "full@example.com"}],
                    "urls": ["https://a.example.com", "https://b.example.com"],
                    "categories": ["Friend", "Work"],
                },
            )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["urls"] == ["https://a.example.com", "https://b.example.com"]
        assert body["categories"] == ["Friend", "Work"]
        assert body["photo"] is None


class TestResolveByEmail:
    def test_resolves_a_contact_by_its_email_case_insensitively(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """`emails` is populated by PostIMAP's own outbound round trip,
        which nothing here triggers -- seeded directly, the same
        convention `test_search_returns_one_row_per_email` above uses."""
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
                        "'BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Resolve Me\r\n"
                        "EMAIL:Resolve@Example.com\r\nEND:VCARD\r\n', "
                        "'Resolve Me', ARRAY['Resolve@Example.com'])"
                    ),
                    {
                        "id": object_id, "account_id": dav_account_id,
                        "collection_id": addressbook_id,
                    },
                )
                await session.commit()

        client.portal.call(_seed_synced_contact, migrated_db)

        with patch(_TARGET, return_value=migrated_db):
            resolved = client.get("/contacts/resolve", params={"email": "resolve@example.com"})
        assert resolved.status_code == 200
        assert resolved.json()["summary"] == "Resolve Me"

    def test_no_match_returns_null_rather_than_an_error(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        with patch(_TARGET, return_value=migrated_db):
            resolved = client.get(
                "/contacts/resolve", params={"email": "nobody@example.com"},
            )
        assert resolved.status_code == 200
        assert resolved.json() is None


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


_IMAGE_EXCEPTIONS_TARGET = "mail_verdict.api.image_exceptions.get_db_connection"


class TestPhotoIndex:
    """`is_sender_image_allowed` (imported from api/image_exceptions.py)
    resolves its own `get_db_connection()` independently of contacts.py's
    -- any test reaching the `kind="url"` branch needs both patched, not
    just `_TARGET`."""

    def test_an_embedded_photo_is_keyed_by_every_email_and_streams_back(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        addressbook_id = client.portal.call(_seed, migrated_db)
        payload = base64.b64encode(b"fake-jpeg-bytes").decode()
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/contacts",
                json={
                    "addressbook_id": str(addressbook_id), "summary": "Photo Person",
                    "emails": [{"email": "work@example.com"}, {"email": "home@example.com"}],
                    "photo_data_url": f"data:image/jpeg;base64,{payload}",
                },
            )
            assert created.status_code == 201, created.text
            contact_id = created.json()["id"]

            index = client.get("/contacts/photo-index")
        assert index.status_code == 200
        by_email = index.json()["by_email"]
        assert by_email["work@example.com"]["contact_id"] == contact_id
        assert by_email["work@example.com"] == by_email["home@example.com"]
        photo_url = by_email["work@example.com"]["photo_url"]
        assert photo_url == f"/api/contacts/{contact_id}/photo"

        with patch(_TARGET, return_value=migrated_db):
            photo = client.get(f"/contacts/{contact_id}/photo")
        assert photo.status_code == 200
        assert photo.headers["content-type"] == "image/jpeg"
        assert photo.content == base64.b64decode(payload)

    async def _seed_url_photo_contact(
        self, db: DatabaseConnection, addressbook_id: uuid.UUID, email: str,
    ) -> uuid.UUID:
        object_id = uuid.uuid4()
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
                    "'BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Url Photo\r\n"
                    "EMAIL:' || :email || '\r\n"
                    "PHOTO;VALUE=URI:https://example.com/photo.jpg\r\nEND:VCARD\r\n', "
                    "'Url Photo', ARRAY[:email])"
                ),
                {
                    "id": object_id, "account_id": dav_account_id,
                    "collection_id": addressbook_id, "email": email,
                },
            )
            await session.commit()
        return object_id

    def test_a_url_photo_is_omitted_with_no_account_given(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        addressbook_id = client.portal.call(_seed, migrated_db)
        client.portal.call(
            self._seed_url_photo_contact, migrated_db, addressbook_id, "url-noacct@example.com",
        )
        with patch(_TARGET, return_value=migrated_db):
            index = client.get("/contacts/photo-index")
        assert index.status_code == 200
        assert "url-noacct@example.com" not in index.json()["by_email"]

    def test_a_url_photo_is_omitted_when_the_sender_is_not_allowlisted(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        addressbook_id = client.portal.call(_seed, migrated_db)
        client.portal.call(
            self._seed_url_photo_contact, migrated_db, addressbook_id, "url-blocked@example.com",
        )
        with patch(_TARGET, return_value=migrated_db), patch(
            _IMAGE_EXCEPTIONS_TARGET, return_value=migrated_db,
        ):
            index = client.get(
                "/contacts/photo-index", params={"account_id": str(uuid.uuid4())},
            )
        assert index.status_code == 200
        assert "url-blocked@example.com" not in index.json()["by_email"]

    async def _seed_account(self, db: DatabaseConnection) -> uuid.UUID:
        account_id = uuid.uuid4()
        async with db.session() as session:
            await session.execute(
                text(
                    "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, "
                    "imap_password) VALUES (:id, :name, 'imap.example.com', 993, "
                    "'user@example.com', '\\x00' || convert_to('pw', 'UTF8'))"
                ),
                {"id": account_id, "name": f"acct-{account_id}"},
            )
            await session.commit()
        return account_id

    def test_a_url_photo_is_included_once_the_sender_is_allowlisted(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        addressbook_id = client.portal.call(_seed, migrated_db)
        contact_id = client.portal.call(
            self._seed_url_photo_contact, migrated_db, addressbook_id, "url-allowed@example.com",
        )
        account_id = client.portal.call(self._seed_account, migrated_db)
        with patch(_IMAGE_EXCEPTIONS_TARGET, return_value=migrated_db):
            exc = client.post(
                f"/accounts/{account_id}/image-exceptions",
                json={"type": "sender", "value": "url-allowed@example.com"},
            )
            assert exc.status_code == 201, exc.text

        with patch(_TARGET, return_value=migrated_db), patch(
            _IMAGE_EXCEPTIONS_TARGET, return_value=migrated_db,
        ):
            index = client.get(
                "/contacts/photo-index", params={"account_id": str(account_id)},
            )
        assert index.status_code == 200
        entry = index.json()["by_email"]["url-allowed@example.com"]
        assert entry["contact_id"] == str(contact_id)
        assert entry["photo_url"] == "https://example.com/photo.jpg"

    def test_the_photo_endpoint_404s_for_a_url_kind_photo(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        addressbook_id = client.portal.call(_seed, migrated_db)
        contact_id = client.portal.call(
            self._seed_url_photo_contact, migrated_db, addressbook_id, "url-404@example.com",
        )
        with patch(_TARGET, return_value=migrated_db):
            photo = client.get(f"/contacts/{contact_id}/photo")
        assert photo.status_code == 404

    def test_the_photo_endpoint_404s_for_an_unknown_contact(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        with patch(_TARGET, return_value=migrated_db):
            photo = client.get(f"/contacts/{uuid.uuid4()}/photo")
        assert photo.status_code == 404


class TestPhotoIndexDoesNotStarveTheServer:
    """The photo-index endpoint scans every contact's vCard for its own
    embedded photo. Proven behaviourally, against a real ASGI app on a
    real event loop -- a handler that touches nothing at all, polled
    while the scan is in flight -- rather than by asserting the source
    calls a particular function, which would pass on code that still
    blocks. The same shape as calendar_events's own
    `test_a_handler_touching_nothing_stays_fast_during_a_calendar_burst`.

    Running the scan on a worker thread does not make the handler free
    of it entirely -- CPython's GIL still hands the interpreter back and
    forth between the event loop thread and the worker thread, so a
    handler touching nothing can still cost tens to a few hundred
    milliseconds under load rather than the low single digits it costs
    standing alone. What the fix rules out is what actually caused the
    outage: seconds of complete unresponsiveness with nothing scheduled
    at all."""

    @pytest.mark.asyncio
    async def test_a_handler_touching_nothing_stays_fast_during_a_photo_scan(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        addressbook_id = await _seed(migrated_db)
        photo_payload = base64.b64encode(os.urandom(40_000)).decode()
        await _seed_many_contacts(
            migrated_db, addressbook_id, count=1200, photo_payload=photo_payload,
        )

        app = FastAPI()
        app.include_router(contacts_router)

        @app.get("/live")
        async def live() -> dict[str, str]:
            """Shaped after the liveness listener in server.py: a literal
            constant, no database, no await on anything at all."""
            return {"status": "alive"}

        live_timings: list[float] = []
        burst_done = asyncio.Event()

        async def poll_live(client: httpx.AsyncClient) -> None:
            # started is captured before the pacing sleep, not after --
            # a stalled event loop delays the sleep's own wakeup exactly
            # as much as it would delay the request that follows it.
            while not burst_done.is_set():
                started = time.perf_counter()
                await asyncio.sleep(0.01)
                try:
                    response = await asyncio.wait_for(client.get("/live"), timeout=5.0)
                except TimeoutError:
                    live_timings.append(time.perf_counter() - started)
                    continue
                live_timings.append(time.perf_counter() - started)
                assert response.status_code == 200

        async def fetch_photo_index(client: httpx.AsyncClient) -> None:
            response = await client.get("/contacts/photo-index")
            assert response.status_code == 200

        with patch(_TARGET, return_value=migrated_db):
            transport = httpx.ASGITransport(app=app)
            # Two separate clients (two separate connection pools) --
            # sharing one would let the pool's own concurrency limit
            # serialize the poller behind the scan, which is a
            # connection-pool artifact, not the event-loop defect this
            # test exists to catch.
            async with (
                httpx.AsyncClient(transport=transport, base_url="http://test") as burst_client,
                httpx.AsyncClient(transport=transport, base_url="http://test") as poll_client,
            ):
                poller = asyncio.create_task(poll_live(poll_client))
                await fetch_photo_index(burst_client)
                burst_done.set()
                await poller

        assert len(live_timings) > 5, (
            "the poller barely ran at all -- the event loop was not free enough "
            "to service it during the scan"
        )
        assert max(live_timings) < 1.0, (
            f"a handler touching nothing took up to {max(live_timings):.2f}s "
            f"to respond while a contacts photo scan was in flight"
        )


class TestListContactsBatchesCollectionLookups:
    def test_a_page_makes_one_collection_lookup_not_one_per_contact(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        addressbook_id = client.portal.call(_seed, migrated_db)
        tiny_photo = base64.b64encode(b"tiny").decode()
        client.portal.call(_seed_many_contacts, migrated_db, addressbook_id, 25, tiny_photo)
        with (
            patch(_TARGET, return_value=migrated_db),
            patch.object(
                CollectionRepository, "get_by_id", new=AsyncMock(return_value=None),
            ) as mock_get_by_id,
        ):
            response = client.get(
                "/contacts", params={"limit": 50, "addressbook_id": str(addressbook_id)},
            )
        assert response.status_code == 200
        assert len(response.json()["contacts"]) == 25
        mock_get_by_id.assert_not_called()


class TestGroupVcardsAreNotListedAsContacts:
    """A Nextcloud address-book group arrives as an ordinary vCard --
    PostIMAP has no concept of one -- and must never be presented as a
    person with no address."""

    async def _seed_group_contact(
        self, db: DatabaseConnection, addressbook_id: uuid.UUID, summary: str,
    ) -> uuid.UUID:
        object_id = uuid.uuid4()
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
                    "'BEGIN:VCARD\r\nVERSION:4.0\r\nKIND:group\r\nFN:' || :summary || "
                    "'\r\nEND:VCARD\r\n', :summary, ARRAY[]::text[])"
                ),
                {
                    "id": object_id, "account_id": dav_account_id,
                    "collection_id": addressbook_id, "summary": summary,
                },
            )
            await session.commit()
        return object_id

    def test_a_group_vcard_is_excluded_from_the_list(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        addressbook_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/contacts",
                json={
                    "addressbook_id": str(addressbook_id), "summary": "Anna Person",
                    "emails": [{"email": "anna@example.com"}],
                },
            )
            assert created.status_code == 201, created.text
            client.portal.call(
                self._seed_group_contact, migrated_db, addressbook_id, "Family Group",
            )

            listed = client.get("/contacts", params={"addressbook_id": str(addressbook_id)})
        assert listed.status_code == 200
        summaries = [c["summary"] for c in listed.json()["contacts"]]
        assert "Anna Person" in summaries
        assert "Family Group" not in summaries
