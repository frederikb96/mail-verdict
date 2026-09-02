"""
Contacts against a real Radicale server, through a real PostIMAP -- the same round
trip test_calendar_flow.py proves for events. dav_objects and dav_collections are
shared tables and nothing about the sync path is calendar-specific, but ContactResponse
carries no `pending`/`etag` field the way EventInstanceOut does, so the "reached the
server" assertion reads dav_objects directly instead of polling the API for it.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import DavObject
from tests.e2e.helpers import (
    wait_for,
    wait_for_async,
    wait_for_dav_account_active,
    wait_for_dav_collection,
)
from tests.setup.containers import RADICALE_ALIAS, RADICALE_PORT
from tests.setup.dav_helpers import (
    create_addressbook,
    discover,
    get_object,
    put_object,
    sample_contact,
)

SEEDED_CONTACTS = {
    f"seed-{i}@e2e.test.local": fn
    for i, fn in enumerate(["Anna Mueller", "Ben Carter"])
}


@pytest.fixture(scope="class")
def radicale_base_url(radicale_endpoint: tuple[str, int]) -> str:
    host, port = radicale_endpoint
    return f"http://{host}:{port}/"


@pytest.fixture(scope="class")
def seeded_addressbook(radicale_base_url: str) -> dict[str, object]:
    """An address book pre-populated directly on the real server before any
    dav_account exists -- the contacts-side counterpart of seeded_calendar."""
    username = f"ab-{uuid.uuid4().hex[:8]}"
    with httpx.Client(auth=(username, "unused"), timeout=10.0) as client:
        principal = discover(client, radicale_base_url)
        addressbook_url = create_addressbook(client, principal, "friends", "Friends")
        for i, (uid, fn) in enumerate(SEEDED_CONTACTS.items()):
            put_object(
                client, f"{addressbook_url}{uid}.vcf",
                sample_contact(uid, fn, f"contact{i}@example.com"),
                "text/vcard; charset=utf-8",
            )
    return {"username": username, "addressbook_url": addressbook_url}


@pytest.fixture(scope="class")
def dav_account(
    app_client: TestClient, seeded_addressbook: dict[str, object],
) -> dict[str, object]:
    resp = app_client.post(
        "/api/dav-accounts",
        json={
            "name": f"Radicale-{uuid.uuid4().hex[:8]}",
            "discovery_url": f"http://{RADICALE_ALIAS}:{RADICALE_PORT}/",
            "username": seeded_addressbook["username"],
            "password": "unused",
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_dav_account_active(app_client, account["id"])
    return account


@pytest.fixture(scope="class")
def addressbook_collection(
    app_client: TestClient, dav_account: dict[str, object],
) -> dict[str, object]:
    return wait_for_dav_collection(app_client, dav_account["id"], "Friends")


async def _read_object_once_synced(
    db: DatabaseConnection, object_id: uuid.UUID,
) -> tuple[str, str | None] | None:
    """(etag, uid) once etag is set -- a non-empty tuple is truthy regardless of what
    it holds, so returning (None, None) while still pending would read as "found" to
    wait_for_async; None until etag lands is what actually waits."""
    async with db.session() as session:
        row = (
            await session.execute(
                select(DavObject.etag, DavObject.uid).where(DavObject.id == object_id)
            )
        ).one()
    return None if row.etag is None else (row.etag, row.uid)


class TestContactsRoundTrip:
    def test_pre_existing_contacts_are_discovered_on_account_creation(
        self, addressbook_collection: dict[str, object],
    ) -> None:
        assert addressbook_collection["total_count"] == len(SEEDED_CONTACTS)

    def test_contacts_seeded_directly_on_the_server_appear_through_the_api(
        self, app_client: TestClient, addressbook_collection: dict[str, object],
    ) -> None:
        resp = app_client.get(
            "/api/contacts", params={"addressbook_id": addressbook_collection["id"]},
        )
        assert resp.status_code == 200, resp.text
        names = {c["summary"] for c in resp.json()["contacts"]}
        assert names == set(SEEDED_CONTACTS.values())

    @pytest.mark.asyncio
    async def test_creating_a_contact_reaches_the_real_server(
        self,
        app_client: TestClient,
        addressbook_collection: dict[str, object],
        seeded_addressbook: dict[str, object],
        db: DatabaseConnection,
    ) -> None:
        resp = app_client.post(
            "/api/contacts",
            json={
                "addressbook_id": addressbook_collection["id"],
                "summary": "Created via API",
                "emails": [{"email": "created@example.com"}],
            },
        )
        assert resp.status_code == 201, resp.text
        contact_id = uuid.UUID(resp.json()["id"])

        _etag, uid = await wait_for_async(
            lambda: _read_object_once_synced(db, contact_id),
            timeout_s=20.0, description=f"dav_objects {contact_id} etag set",
        )
        assert uid is not None

        with httpx.Client(auth=(seeded_addressbook["username"], "unused"), timeout=10.0) as client:
            body = get_object(client, f"{seeded_addressbook['addressbook_url']}{uid}.vcf")
        assert "FN:Created via API" in body

    def test_a_contact_added_on_the_server_appears_through_the_api_after_sync(
        self,
        app_client: TestClient,
        dav_account: dict[str, object],
        addressbook_collection: dict[str, object],
        seeded_addressbook: dict[str, object],
    ) -> None:
        uid = f"server-side-{uuid.uuid4().hex[:8]}@e2e.test.local"
        with httpx.Client(auth=(seeded_addressbook["username"], "unused"), timeout=10.0) as client:
            put_object(
                client, f"{seeded_addressbook['addressbook_url']}{uid}.vcf",
                sample_contact(uid, "Chidi Okafor", "chidi@example.net"),
                "text/vcard; charset=utf-8",
            )

        sync_resp = app_client.post(f"/api/dav-accounts/{dav_account['id']}/sync")
        assert sync_resp.status_code == 200, sync_resp.text

        def _check() -> dict[str, object] | None:
            resp = app_client.get(
                "/api/contacts", params={"addressbook_id": addressbook_collection["id"]},
            )
            assert resp.status_code == 200, resp.text
            return next(
                (c for c in resp.json()["contacts"] if c["summary"] == "Chidi Okafor"), None,
            )

        found = wait_for(
            _check, timeout_s=30.0, description="Server-side contact synced into MailVerdict",
        )
        assert any(e["email"] == "chidi@example.net" for e in found["emails"])
