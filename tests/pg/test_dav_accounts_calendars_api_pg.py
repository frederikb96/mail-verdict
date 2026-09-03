"""
DAV account and calendar API endpoints, against a real database and a
real PostIMAP (which is what actually grants the dav_* writes these
routers issue).
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

from mail_verdict.api.calendars import addressbooks_router, links_router
from mail_verdict.api.calendars import router as calendars_router
from mail_verdict.api.dav_accounts import router as dav_accounts_router
from mail_verdict.api.identities import router as identities_router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Identity
from mail_verdict.postimap.actions import force_reconnect_dav_account


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(dav_accounts_router)
    app.include_router(calendars_router)
    app.include_router(links_router)
    app.include_router(addressbooks_router)
    app.include_router(identities_router)
    with TestClient(app) as c:
        yield c


_DAV_ACCOUNTS_TARGET = "mail_verdict.api.dav_accounts.get_db_connection"
_CALENDARS_TARGET = "mail_verdict.api.calendars.get_db_connection"
_IDENTITIES_TARGET = "mail_verdict.api.identities.get_db_connection"


async def _seed_dav_account(session: AsyncSession) -> uuid.UUID:
    dav_account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dav_accounts (id, name, url, username, password) "
            "VALUES (:id, :name, 'https://dav.example.com/', 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": dav_account_id, "name": f"dav-{dav_account_id}"},
    )
    return dav_account_id


async def _seed_mail_account_and_identity(
    session: AsyncSession, email: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    identity_id = uuid.uuid4()
    session.add(Identity(id=identity_id, account_id=account_id, email=email))
    await session.flush()
    return account_id, identity_id


class TestDavAccounts:
    def test_create_and_list_dav_account(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        name = f"Nextcloud-{uuid.uuid4()}"
        with patch(_DAV_ACCOUNTS_TARGET, return_value=migrated_db):
            created = client.post(
                "/dav-accounts",
                json={
                    "name": name, "discovery_url": "https://cloud.example.org/dav/",
                    "username": "alice", "password": "an-app-password",
                },
            )
            assert created.status_code == 201, created.text
            body = created.json()
            assert body["name"] == name
            assert body["discovery_url"] == "https://cloud.example.org/dav/"
            assert body["collections"] == []

            listed = client.get("/dav-accounts")
        assert listed.status_code == 200
        assert any(a["id"] == body["id"] for a in listed.json())

    def test_update_and_delete_dav_account(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        with patch(_DAV_ACCOUNTS_TARGET, return_value=migrated_db):
            created = client.post(
                "/dav-accounts",
                json={
                    "name": "ToRename", "discovery_url": "https://cloud.example.org/dav/",
                    "username": "alice", "password": "pw",
                },
            )
            dav_account_id = created.json()["id"]

            updated = client.patch(f"/dav-accounts/{dav_account_id}", json={"is_active": False})
            assert updated.status_code == 200
            assert updated.json()["is_active"] is False

            deleted = client.delete(f"/dav-accounts/{dav_account_id}")
            assert deleted.status_code == 204

            gone = client.get(f"/dav-accounts/{dav_account_id}")
        assert gone.status_code == 404

    def test_get_unknown_dav_account_is_404(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        with patch(_DAV_ACCOUNTS_TARGET, return_value=migrated_db):
            resp = client.get(f"/dav-accounts/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_password_change_on_an_active_account_forces_reconnect(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """Correcting a wrong password on an already-running
        account has to actually reconnect PostIMAP to it -- the
        documented mail-side trap (api/accounts.py's own
        credentials_changed/was_active dance), not carried across to DAV
        accounts until this."""
        with patch(_DAV_ACCOUNTS_TARGET, return_value=migrated_db):
            created = client.post(
                "/dav-accounts",
                json={
                    "name": f"Nextcloud-{uuid.uuid4()}",
                    "discovery_url": "https://cloud.example.org/dav/",
                    "username": "alice", "password": "wrong-password",
                },
            )
            dav_account_id = created.json()["id"]
            assert created.json()["is_active"] is True

            with patch(
                "mail_verdict.api.dav_accounts.force_reconnect_dav_account",
                wraps=force_reconnect_dav_account,
            ) as spy:
                updated = client.patch(
                    f"/dav-accounts/{dav_account_id}", json={"password": "correct-password"},
                )
            assert updated.status_code == 200
            spy.assert_called_once()

            after = client.get(f"/dav-accounts/{dav_account_id}")
        assert after.status_code == 200
        # The bounce ends the account active again, not stuck off.
        assert after.json()["is_active"] is True

    def test_renaming_does_not_force_reconnect(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        with patch(_DAV_ACCOUNTS_TARGET, return_value=migrated_db):
            created = client.post(
                "/dav-accounts",
                json={
                    "name": f"Nextcloud-{uuid.uuid4()}",
                    "discovery_url": "https://cloud.example.org/dav/",
                    "username": "alice", "password": "pw",
                },
            )
            dav_account_id = created.json()["id"]

            with patch(
                "mail_verdict.api.dav_accounts.force_reconnect_dav_account",
                wraps=force_reconnect_dav_account,
            ) as spy:
                updated = client.patch(
                    f"/dav-accounts/{dav_account_id}", json={"name": "Renamed"},
                )
            assert updated.status_code == 200
        spy.assert_not_called()


class TestCalendars:
    def test_create_list_and_update_calendar(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        dav_account_id = client.portal.call(self._seed, migrated_db)

        with patch(_CALENDARS_TARGET, return_value=migrated_db):
            created = client.post(
                "/calendars",
                json={
                    "dav_account_id": str(dav_account_id), "display_name": "Work",
                    "color": "#0082C9",
                },
            )
            assert created.status_code == 201, created.text
            calendar_id = created.json()["id"]
            assert created.json()["intake"] == "none"
            assert created.json()["is_visible"] is True

            listed = client.get("/calendars")
            assert any(c["id"] == calendar_id for c in listed.json())

            updated = client.patch(
                f"/calendars/{calendar_id}",
                json={"color_override": "#ff0000", "is_visible": False},
            )
        assert updated.status_code == 200
        assert updated.json()["color_override"] == "#ff0000"
        assert updated.json()["is_visible"] is False

    async def _seed(self, db: DatabaseConnection) -> uuid.UUID:
        async with db.session() as session:
            dav_account_id = await _seed_dav_account(session)
            await session.commit()
        return dav_account_id

    def test_delete_requires_confirmation(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        dav_account_id = client.portal.call(self._seed, migrated_db)
        with patch(_CALENDARS_TARGET, return_value=migrated_db):
            created = client.post(
                "/calendars",
                json={"dav_account_id": str(dav_account_id), "display_name": "Delete me"},
            )
            calendar_id = created.json()["id"]

            without_confirm = client.delete(f"/calendars/{calendar_id}")
            assert without_confirm.status_code == 409

            with_confirm = client.delete(f"/calendars/{calendar_id}?confirm_event_count=0")
        assert with_confirm.status_code == 204

    def test_intake_requires_an_identity(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        dav_account_id = client.portal.call(self._seed, migrated_db)
        with patch(_CALENDARS_TARGET, return_value=migrated_db):
            created = client.post(
                "/calendars",
                json={"dav_account_id": str(dav_account_id), "display_name": "No identity"},
            )
            calendar_id = created.json()["id"]

            resp = client.patch(f"/calendars/{calendar_id}", json={"intake": "import_and_link"})
        assert resp.status_code == 400


class TestAddressbooks:
    async def _seed(self, db: DatabaseConnection) -> uuid.UUID:
        async with db.session() as session:
            dav_account_id = await _seed_dav_account(session)
            await session.commit()
        return dav_account_id

    def test_create_and_list_addressbook(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        dav_account_id = client.portal.call(self._seed, migrated_db)

        with patch(_CALENDARS_TARGET, return_value=migrated_db):
            created = client.post(
                "/addressbooks",
                json={"dav_account_id": str(dav_account_id), "display_name": "Contacts"},
            )
            assert created.status_code == 201, created.text
            addressbook_id = created.json()["id"]
            assert created.json()["display_name"] == "Contacts"
            assert created.json()["total_count"] == 0

            listed = client.get("/addressbooks")
        assert any(a["id"] == addressbook_id for a in listed.json())

    def test_create_with_unknown_dav_account_is_404(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        with patch(_CALENDARS_TARGET, return_value=migrated_db):
            resp = client.post(
                "/addressbooks",
                json={"dav_account_id": str(uuid.uuid4()), "display_name": "Ghost"},
            )
        assert resp.status_code == 404


class TestCalendarLinks:
    async def _seed_identity(self, db: DatabaseConnection, email: str) -> uuid.UUID:
        async with db.session() as session:
            _account_id, identity_id = await _seed_mail_account_and_identity(session, email)
            await session.commit()
        return identity_id

    async def _seed_calendar(self, db: DatabaseConnection) -> uuid.UUID:
        async with db.session() as session:
            dav_account_id = await _seed_dav_account(session)
            collection_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO dav_collections (id, account_id, kind, slug, display_name) "
                    "VALUES (:id, :account_id, 'calendar', 'work', 'Work')"
                ),
                {"id": collection_id, "account_id": dav_account_id},
            )
            await session.commit()
        return collection_id

    def test_get_reports_every_identity_even_unlinked(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        identity_id = client.portal.call(self._seed_identity, migrated_db, "freddy@work.example")
        with patch(_CALENDARS_TARGET, return_value=migrated_db):
            resp = client.get("/calendar/links")
        assert resp.status_code == 200
        body = resp.json()
        row = next(r for r in body["rows"] if r["identity_id"] == str(identity_id))
        assert row["calendar_ids"] == []
        assert row["receives_invitations_calendar_id"] is None

    def test_put_links_a_calendar_and_sets_intake(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        identity_id = client.portal.call(self._seed_identity, migrated_db, "freddy@work.example")
        calendar_id = client.portal.call(self._seed_calendar, migrated_db)

        with patch(_CALENDARS_TARGET, return_value=migrated_db):
            current = client.get("/calendar/links").json()
            put = client.put(
                "/calendar/links",
                json={
                    "base_revision": current["base_revision"],
                    "rows": [
                        {
                            "identity_id": str(identity_id),
                            "calendar_ids": [str(calendar_id)],
                            "receives_invitations_calendar_id": str(calendar_id),
                        },
                    ],
                },
            )
        assert put.status_code == 200, put.text
        row = next(r for r in put.json()["rows"] if r["identity_id"] == str(identity_id))
        assert row["calendar_ids"] == [str(calendar_id)]
        assert row["receives_invitations_calendar_id"] == str(calendar_id)
        assert put.json()["base_revision"] == current["base_revision"] + 1

        with patch(_CALENDARS_TARGET, return_value=migrated_db):
            calendars = client.get("/calendars").json()
        updated_calendar = next(c for c in calendars if c["id"] == str(calendar_id))
        assert updated_calendar["identity_id"] == str(identity_id)
        assert updated_calendar["intake"] == "import_and_link"

    def test_put_with_a_calendar_and_no_intake_choice_is_rejected(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        identity_id = client.portal.call(self._seed_identity, migrated_db, "freddy@work.example")
        calendar_id = client.portal.call(self._seed_calendar, migrated_db)
        with patch(_CALENDARS_TARGET, return_value=migrated_db):
            current = client.get("/calendar/links").json()
            put = client.put(
                "/calendar/links",
                json={
                    "base_revision": current["base_revision"],
                    "rows": [
                        {
                            "identity_id": str(identity_id),
                            "calendar_ids": [str(calendar_id)],
                            "receives_invitations_calendar_id": None,
                        },
                    ],
                },
            )
        assert put.status_code == 422

    def test_put_with_a_stale_base_revision_is_409(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        with patch(_CALENDARS_TARGET, return_value=migrated_db):
            put = client.put(
                "/calendar/links", json={"base_revision": 9999, "rows": []},
            )
        assert put.status_code == 409
