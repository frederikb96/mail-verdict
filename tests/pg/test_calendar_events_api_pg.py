"""Calendar event API endpoints, against a real database and a real PostIMAP."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.calendar_events import router as events_router
from mail_verdict.api.calendars import router as calendars_router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Identity

_TARGET = "mail_verdict.api.calendar_events.get_db_connection"
_CALENDARS_TARGET = "mail_verdict.api.calendars.get_db_connection"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(events_router)
    app.include_router(calendars_router)
    with TestClient(app) as c:
        yield c


async def _seed_calendar(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
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
            "VALUES (:id, :account_id, 'calendar', 'work', 'Work')"
        ),
        {"id": collection_id, "account_id": dav_account_id},
    )
    return dav_account_id, collection_id


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


async def _seed(db: DatabaseConnection) -> uuid.UUID:
    async with db.session() as session:
        _dav_account_id, collection_id = await _seed_calendar(session)
        await session.commit()
    return collection_id


class TestCreateAndList:
    def test_create_and_list_a_simple_event(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        calendar_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/calendar/events",
                json={
                    "calendar_id": str(calendar_id), "summary": "Planning",
                    "dtstart": "2026-09-10T10:00:00+00:00",
                    "dtend": "2026-09-10T11:00:00+00:00",
                },
            )
            assert created.status_code == 201, created.text
            body = created.json()
            assert body["summary"] == "Planning"
            assert body["pending"] is True
            assert body["calendar_id"] == str(calendar_id)

            listed = client.get(
                "/calendar/events", params={"month": "2026-09", "calendars": str(calendar_id)},
            )
        assert listed.status_code == 200
        summaries = [e["summary"] for e in listed.json()["events"]]
        assert "Planning" in summaries

    def test_list_expands_a_recurring_series(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        calendar_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/calendar/events",
                json={
                    "calendar_id": str(calendar_id), "summary": "Standup",
                    "dtstart": "2026-09-01T09:00:00+00:00",
                    "dtend": "2026-09-01T09:15:00+00:00",
                    "rrule": "FREQ=WEEKLY;COUNT=4",
                },
            )
            assert created.status_code == 201, created.text

            listed = client.get(
                "/calendar/events", params={"month": "2026-09", "calendars": str(calendar_id)},
            )
        assert listed.status_code == 200
        standups = [e for e in listed.json()["events"] if e["summary"] == "Standup"]
        assert len(standups) == 4
        assert all(e["is_recurring"] for e in standups)

    def test_get_unknown_event_is_404(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        with patch(_TARGET, return_value=migrated_db):
            resp = client.get(f"/calendar/events/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestCreateWithAttendees:
    async def _seed_identity_and_link(
        self, db: DatabaseConnection, calendar_id: uuid.UUID,
    ) -> uuid.UUID:
        async with db.session() as session:
            _account_id, identity_id = await _seed_mail_account_and_identity(
                session, "freddy@work.example",
            )
            await session.execute(
                text(
                    "INSERT INTO calendar_prefs (collection_id, identity_id) "
                    "VALUES (:collection_id, :identity_id)"
                ),
                {"collection_id": calendar_id, "identity_id": identity_id},
            )
            await session.commit()
        return identity_id

    def test_create_with_attendees_requires_a_linked_identity(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        calendar_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            resp = client.post(
                "/calendar/events",
                json={
                    "calendar_id": str(calendar_id), "summary": "Kickoff",
                    "dtstart": "2026-09-10T10:00:00+00:00",
                    "dtend": "2026-09-10T11:00:00+00:00",
                    "attendees": [{"email": "anna@example.com"}],
                },
            )
        assert resp.status_code == 409

    def test_create_with_attendees_sends_a_request_and_records_organizer(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        calendar_id = client.portal.call(_seed, migrated_db)
        identity_id = client.portal.call(self._seed_identity_and_link, migrated_db, calendar_id)

        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/calendar/events",
                json={
                    "calendar_id": str(calendar_id), "summary": "Kickoff",
                    "dtstart": "2026-09-10T10:00:00+00:00",
                    "dtend": "2026-09-10T11:00:00+00:00",
                    "attendees": [{"email": "anna@example.com", "cn": "Anna"}],
                },
            )
        assert created.status_code == 201, created.text
        assert created.json()["organizer"]["email"] == "freddy@work.example"
        assert len(created.json()["attendees"]) == 1

        async def _check_outbox(db: DatabaseConnection) -> int:
            async with db.session() as session:
                result = await session.execute(
                    text(
                        "SELECT count(*) FROM outbox WHERE from_addr = 'freddy@work.example' "
                        "AND 'anna@example.com' = ANY(SELECT jsonb_array_elements_text(to_addrs))"
                    ),
                )
                return result.scalar_one()

        count = client.portal.call(_check_outbox, migrated_db)
        assert count == 1
        assert identity_id  # identity used above


class TestUpdateAndDelete:
    def test_update_scope_all_renames_and_bumps_sequence(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        calendar_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/calendar/events",
                json={
                    "calendar_id": str(calendar_id), "summary": "Original",
                    "dtstart": "2026-09-10T10:00:00+00:00",
                    "dtend": "2026-09-10T11:00:00+00:00",
                },
            )
            object_id = created.json()["object_id"]

            updated = client.patch(
                f"/calendar/events/{object_id}",
                json={"summary": "Renamed", "scope": "all"},
            )
        assert updated.status_code == 200, updated.text
        assert updated.json()["summary"] == "Renamed"
        assert updated.json()["sequence"] == 1

    def test_update_scope_this_edits_one_occurrence_only(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        calendar_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/calendar/events",
                json={
                    "calendar_id": str(calendar_id), "summary": "Standup",
                    "dtstart": "2026-09-01T09:00:00+00:00",
                    "dtend": "2026-09-01T09:15:00+00:00",
                    "rrule": "FREQ=WEEKLY;COUNT=4",
                },
            )
            object_id = created.json()["object_id"]

            listed = client.get(
                "/calendar/events", params={"month": "2026-09", "calendars": str(calendar_id)},
            )
            second = listed.json()["events"][1]

            updated = client.patch(
                f"/calendar/events/{object_id}",
                json={
                    "summary": "Standup (special)", "scope": "this",
                    "recurrence_id": second["recurrence_id"],
                },
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["summary"] == "Standup (special)"

            relisted = client.get(
                "/calendar/events", params={"month": "2026-09", "calendars": str(calendar_id)},
            )
        summaries = [e["summary"] for e in relisted.json()["events"]]
        assert summaries.count("Standup") == 3
        assert summaries.count("Standup (special)") == 1

    def test_update_scope_following_is_rejected(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        calendar_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/calendar/events",
                json={
                    "calendar_id": str(calendar_id), "summary": "Standup",
                    "dtstart": "2026-09-01T09:00:00+00:00",
                    "dtend": "2026-09-01T09:15:00+00:00",
                    "rrule": "FREQ=WEEKLY;COUNT=4",
                },
            )
            object_id = created.json()["object_id"]
            resp = client.patch(
                f"/calendar/events/{object_id}", json={"summary": "x", "scope": "following"},
            )
        assert resp.status_code == 422

    def test_delete_removes_the_event(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        calendar_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/calendar/events",
                json={
                    "calendar_id": str(calendar_id), "summary": "Delete me",
                    "dtstart": "2026-09-10T10:00:00+00:00",
                    "dtend": "2026-09-10T11:00:00+00:00",
                },
            )
            object_id = created.json()["object_id"]

            deleted = client.delete(f"/calendar/events/{object_id}")
            assert deleted.status_code == 204

            gone = client.get(f"/calendar/events/{object_id}")
        assert gone.status_code == 404

    def test_delete_scope_this_cancels_rather_than_removes(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        calendar_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/calendar/events",
                json={
                    "calendar_id": str(calendar_id), "summary": "Standup",
                    "dtstart": "2026-09-01T09:00:00+00:00",
                    "dtend": "2026-09-01T09:15:00+00:00",
                    "rrule": "FREQ=WEEKLY;COUNT=4",
                },
            )
            object_id = created.json()["object_id"]
            listed = client.get(
                "/calendar/events", params={"month": "2026-09", "calendars": str(calendar_id)},
            )
            second = listed.json()["events"][1]

            # TestClient.delete() has no json= parameter (httpx/starlette
            # keep DELETE bodies out of the convenience methods) -- send it
            # through the generic request() instead.
            deleted = client.request(
                "DELETE", f"/calendar/events/{object_id}",
                json={"scope": "this", "recurrence_id": second["recurrence_id"]},
            )
            assert deleted.status_code == 204

            relisted = client.get(
                "/calendar/events", params={"month": "2026-09", "calendars": str(calendar_id)},
            )
        statuses = {e["recurrence_id"]: e["status"] for e in relisted.json()["events"]}
        assert statuses[second["recurrence_id"]] == "cancelled"
        # The object still exists (soft-cancelled, not deleted).
        still_there = client.get(
            f"/calendar/events/{object_id}", params={}, follow_redirects=True,
        )
        assert still_there.status_code == 200


class TestRespond:
    async def _seed_invitation(
        self, db: DatabaseConnection,
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        """A calendar holding an event this identity is invited to, as an
        attendee -- the shape intake would have produced."""
        async with db.session() as session:
            dav_account_id, collection_id = await _seed_calendar(session)
            _account_id, identity_id = await _seed_mail_account_and_identity(
                session, "freddy@work.example",
            )
            object_id = uuid.uuid4()
            data = (
                "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
                "UID:invite-1\r\nDTSTAMP:20260901T120000Z\r\n"
                "DTSTART:20260910T090000Z\r\nDTEND:20260910T100000Z\r\n"
                "SUMMARY:Kickoff\r\nSEQUENCE:0\r\n"
                "ORGANIZER;CN=Anna:mailto:anna@example.com\r\n"
                "ATTENDEE;CN=Freddy;PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT:"
                "mailto:freddy@work.example\r\n"
                "END:VEVENT\r\nEND:VCALENDAR\r\n"
            )
            await session.execute(
                text(
                    "INSERT INTO dav_objects (id, account_id, collection_id, kind, data) "
                    "VALUES (:id, :account_id, :collection_id, 'calendar', :data)"
                ),
                {
                    "id": object_id, "account_id": dav_account_id,
                    "collection_id": collection_id, "data": data,
                },
            )
            await session.commit()
        return collection_id, object_id, identity_id

    def test_respond_accepted_updates_partstat_and_sends_reply(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        _calendar_id, object_id, identity_id = client.portal.call(
            self._seed_invitation, migrated_db,
        )
        with patch(_TARGET, return_value=migrated_db):
            resp = client.post(
                f"/calendar/events/{object_id}/respond",
                json={"identity_id": str(identity_id), "partstat": "accepted"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["partstat"] == "accepted"
        assert body["own_reply"] is not None
        assert body["own_reply"]["partstat"] == "accepted"

        async def _outbox_row(db: DatabaseConnection) -> tuple[str, list[str]]:
            async with db.session() as session:
                result = await session.execute(
                    text("SELECT from_addr, to_addrs FROM outbox WHERE id = :id"),
                    {"id": uuid.UUID(body["own_reply"]["outbox_id"])},
                )
                row = result.one()
                return row.from_addr, row.to_addrs

        from_addr, to_addrs = client.portal.call(_outbox_row, migrated_db)
        assert from_addr == "freddy@work.example"
        assert to_addrs == ["anna@example.com"]

    def test_respond_from_a_non_attendee_is_refused(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        _calendar_id, object_id, _identity_id = client.portal.call(
            self._seed_invitation, migrated_db,
        )
        async def _seed_other(db: DatabaseConnection) -> uuid.UUID:
            async with db.session() as session:
                _account_id, other_identity_id = await _seed_mail_account_and_identity(
                    session, "notinvited@example.com",
                )
                await session.commit()
            return other_identity_id

        other_identity_id = client.portal.call(_seed_other, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            resp = client.post(
                f"/calendar/events/{object_id}/respond",
                json={"identity_id": str(other_identity_id), "partstat": "accepted"},
            )
        assert resp.status_code == 409
