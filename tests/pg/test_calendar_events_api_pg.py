"""Calendar event API endpoints, against a real database and a real PostIMAP."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.calendar_events import router as events_router
from mail_verdict.api.calendars import router as calendars_router
from mail_verdict.calendar import ical
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Identity

_TARGET = "mail_verdict.api.calendar_events.get_db_connection"
_CALENDARS_TARGET = "mail_verdict.api.calendars.get_db_connection"

# Shaped like what a real CalDAV server (Nextcloud, or an Outlook
# invitation) actually emits for a timezone-bound recurring series: a
# VTIMEZONE component, an RRULE narrowed by an EXDATE, an RDATE adding an
# extra occurrence, an exception overriding one occurrence, and
# properties this codebase never models (CATEGORIES, an X- property, a
# VALARM subcomponent). Row 125's round-trip proof for the PATCH endpoint
# itself, not just the ical.py functions underneath it.
_EXOTIC_RECURRING_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VTIMEZONE\r\n"
    "TZID:Europe/Berlin\r\n"
    "BEGIN:DAYLIGHT\r\n"
    "TZOFFSETFROM:+0100\r\n"
    "TZOFFSETTO:+0200\r\n"
    "TZNAME:CEST\r\n"
    "DTSTART:19700329T020000\r\n"
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\r\n"
    "END:DAYLIGHT\r\n"
    "BEGIN:STANDARD\r\n"
    "TZOFFSETFROM:+0200\r\n"
    "TZOFFSETTO:+0100\r\n"
    "TZNAME:CET\r\n"
    "DTSTART:19701025T030000\r\n"
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\r\n"
    "END:STANDARD\r\n"
    "END:VTIMEZONE\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:exotic-pg-1\r\n"
    "DTSTAMP:20260901T120000Z\r\n"
    "DTSTART;TZID=Europe/Berlin:20260907T090000\r\n"
    "DTEND;TZID=Europe/Berlin:20260907T100000\r\n"
    "SUMMARY:Weekly standup\r\n"
    "CATEGORIES:WORK,PLANNING\r\n"
    "SEQUENCE:0\r\n"
    "X-CUSTOM-CLIENT-ID:abc-123\r\n"
    "RRULE:FREQ=WEEKLY;COUNT=6\r\n"
    "EXDATE;TZID=Europe/Berlin:20260914T090000\r\n"
    "RDATE;TZID=Europe/Berlin:20261020T130000\r\n"
    "BEGIN:VALARM\r\n"
    "ACTION:DISPLAY\r\n"
    "DESCRIPTION:Reminder\r\n"
    "TRIGGER:-PT15M\r\n"
    "END:VALARM\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:exotic-pg-1\r\n"
    "RECURRENCE-ID;TZID=Europe/Berlin:20260921T090000\r\n"
    "DTSTAMP:20260901T120000Z\r\n"
    "DTSTART;TZID=Europe/Berlin:20260921T100000\r\n"
    "DTEND;TZID=Europe/Berlin:20260921T110000\r\n"
    "SUMMARY:Weekly standup (moved)\r\n"
    "SEQUENCE:1\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


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


async def _seed_identity_and_link(
    db: DatabaseConnection, calendar_id: uuid.UUID, email: str = "freddy@work.example",
) -> uuid.UUID:
    async with db.session() as session:
        _account_id, identity_id = await _seed_mail_account_and_identity(session, email)
        await session.execute(
            text(
                "INSERT INTO calendar_prefs (collection_id, identity_id) "
                "VALUES (:collection_id, :identity_id)"
            ),
            {"collection_id": calendar_id, "identity_id": identity_id},
        )
        await session.commit()
    return identity_id


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


class TestCreateWithTimezone:
    """Row 146: creating an event with tz either honours it or refuses
    it -- through the actual POST endpoint, not just ical.build_new_event
    directly."""

    def test_create_with_tz_stores_a_named_zone_dtstart(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        calendar_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/calendar/events",
                json={
                    "calendar_id": str(calendar_id), "summary": "Standup",
                    "dtstart": "2026-09-10T10:00:00+00:00",
                    "dtend": "2026-09-10T11:00:00+00:00",
                    "tz": "Europe/Berlin",
                },
            )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["tz"] == "Europe/Berlin"
        # The wall-clock reading given (10:00) is kept; only the zone it
        # resolves against changed -- Europe/Berlin's own CEST offset in
        # September (+02:00), not the fixed +00:00 the request carried --
        # so the instant moved from 10:00 UTC to 08:00 UTC.
        returned_dtstart = datetime.fromisoformat(body["dtstart"])
        assert returned_dtstart.utcoffset() == timedelta(hours=2)
        assert returned_dtstart.astimezone(timezone.utc) == datetime(
            2026, 9, 10, 8, 0, tzinfo=timezone.utc,
        )

        object_id = body["object_id"]

        async def _raw_data(db: DatabaseConnection) -> str:
            async with db.session() as session:
                result = await session.execute(
                    text("SELECT data FROM dav_objects WHERE id = :id"), {"id": object_id},
                )
                return str(result.scalar_one())

        raw = client.portal.call(_raw_data, migrated_db)
        assert "DTSTART;TZID=Europe/Berlin:20260910T100000" in raw

    def test_create_with_an_unknown_tz_is_refused(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        calendar_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            resp = client.post(
                "/calendar/events",
                json={
                    "calendar_id": str(calendar_id), "summary": "Standup",
                    "dtstart": "2026-09-10T10:00:00+00:00",
                    "dtend": "2026-09-10T11:00:00+00:00",
                    "tz": "Not/AZone",
                },
            )
        assert resp.status_code == 400, resp.text

    def test_create_with_tz_and_all_day_is_refused(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        calendar_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            resp = client.post(
                "/calendar/events",
                json={
                    "calendar_id": str(calendar_id), "summary": "Holiday",
                    "dtstart": "2026-09-10T00:00:00+00:00",
                    "dtend": "2026-09-11T00:00:00+00:00",
                    "all_day": True,
                    "tz": "Europe/Berlin",
                },
            )
        assert resp.status_code == 400, resp.text


class TestMalformedObjectResilience:
    """A re-verification found the RRULE occurrence guard's own bug: two
    RRULE lines on one VEVENT raised AttributeError, uncaught by
    list_events' `except ValueError`, so the whole month view 500'd for
    every visible calendar rather than just excluding the one object."""

    def test_an_unparseable_object_does_not_500_the_month_view(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        calendar_id = client.portal.call(_seed, migrated_db)

        async def _seed_two_rrule_object(db: DatabaseConnection) -> uuid.UUID:
            async with db.session() as session:
                dav_account_id = (
                    await session.execute(
                        text("SELECT account_id FROM dav_collections WHERE id = :id"),
                        {"id": calendar_id},
                    )
                ).scalar_one()
                object_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO dav_objects (id, account_id, collection_id, kind, data) "
                        "VALUES (:id, :account_id, :collection_id, 'calendar', "
                        "'BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:two-rrules\r\n"
                        "DTSTART:20260901T090000Z\r\nDTEND:20260901T100000Z\r\n"
                        "SUMMARY:Double-ruled\r\nSEQUENCE:0\r\n"
                        "RRULE:FREQ=DAILY\r\nRRULE:FREQ=WEEKLY\r\n"
                        "END:VEVENT\r\nEND:VCALENDAR')"
                    ),
                    {
                        "id": object_id, "account_id": dav_account_id,
                        "collection_id": calendar_id,
                    },
                )
                await session.commit()
            return object_id

        client.portal.call(_seed_two_rrule_object, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/calendar/events",
                json={
                    "calendar_id": str(calendar_id), "summary": "Ordinary",
                    "dtstart": "2026-09-15T10:00:00+00:00",
                    "dtend": "2026-09-15T11:00:00+00:00",
                },
            )
            assert created.status_code == 201, created.text

            listed = client.get(
                "/calendar/events", params={"month": "2026-09", "calendars": str(calendar_id)},
            )
        # The point is only that the request survives an object the
        # occurrence bound has to interpret two RRULE lines for --
        # whatever recurring-ical-events itself makes of that is between
        # it and RFC 5545, not something this test judges.
        assert listed.status_code == 200, listed.text
        summaries = [e["summary"] for e in listed.json()["events"]]
        assert "Ordinary" in summaries


class TestWriteErrors:
    """Row 110: a reverted write -- the server's copy already overwrote
    the user's edit -- has to say so, not read as "nothing happened"."""

    def test_reverted_write_is_surfaced_with_its_own_wording(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        async def _seed(db: DatabaseConnection) -> uuid.UUID:
            async with db.session() as session:
                dav_account_id, collection_id = await _seed_calendar(session)
                object_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO dav_objects (id, account_id, collection_id, kind, data) "
                        "VALUES (:id, :account_id, :collection_id, 'calendar', "
                        "'BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:reverted-1\r\n"
                        "DTSTART:20260910T090000Z\r\nDTEND:20260910T100000Z\r\nSUMMARY:Kickoff\r\n"
                        "END:VEVENT\r\nEND:VCALENDAR')"
                    ),
                    {"id": object_id, "account_id": dav_account_id, "collection_id": collection_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO dav_notifications "
                        "(account_id, action, object_id, error, reverted_at) "
                        "VALUES (:account_id, 'put', :object_id, 'stale etag', now())"
                    ),
                    {"account_id": dav_account_id, "object_id": object_id},
                )
                await session.commit()
            return object_id

        object_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            resp = client.get(f"/calendar/events/{object_id}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["sync_error"] is not None
        assert "replaced" in resp.json()["sync_error"].lower()

    def test_unresolved_write_keeps_the_servers_own_error(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        async def _seed(db: DatabaseConnection) -> uuid.UUID:
            async with db.session() as session:
                dav_account_id, collection_id = await _seed_calendar(session)
                object_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO dav_objects (id, account_id, collection_id, kind, data) "
                        "VALUES (:id, :account_id, :collection_id, 'calendar', "
                        "'BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:unresolved-1\r\n"
                        "DTSTART:20260910T090000Z\r\nDTEND:20260910T100000Z\r\nSUMMARY:Kickoff\r\n"
                        "END:VEVENT\r\nEND:VCALENDAR')"
                    ),
                    {"id": object_id, "account_id": dav_account_id, "collection_id": collection_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO dav_notifications "
                        "(account_id, action, object_id, error) "
                        "VALUES (:account_id, 'put', :object_id, 'connection refused')"
                    ),
                    {"account_id": dav_account_id, "object_id": object_id},
                )
                await session.commit()
            return object_id

        object_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            resp = client.get(f"/calendar/events/{object_id}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["sync_error"] == "connection refused"


class TestCreateWithAttendees:
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
        identity_id = client.portal.call(_seed_identity_and_link, migrated_db, calendar_id)

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

    def test_update_as_attendee_does_not_bump_sequence(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """Row 114: SEQUENCE is the organizer's own version counter --
        editing an event this calendar was only invited to, not created,
        must not advance it, or the real organizer's next genuine update
        loses to it as stale."""
        calendar_id = client.portal.call(_seed, migrated_db)
        client.portal.call(_seed_identity_and_link, migrated_db, calendar_id)

        async def _seed_received_invitation(db: DatabaseConnection) -> uuid.UUID:
            async with db.session() as session:
                dav_account_id = (
                    await session.execute(
                        text("SELECT account_id FROM dav_collections WHERE id = :id"),
                        {"id": calendar_id},
                    )
                ).scalar_one()
                object_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO dav_objects (id, account_id, collection_id, kind, data) "
                        "VALUES (:id, :account_id, :collection_id, 'calendar', "
                        "'BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:received-1\r\n"
                        "DTSTART:20260910T090000Z\r\nDTEND:20260910T100000Z\r\nSUMMARY:Kickoff\r\n"
                        "SEQUENCE:0\r\nORGANIZER:mailto:anna@example.com\r\n"
                        "ATTENDEE:mailto:freddy@work.example\r\nEND:VEVENT\r\nEND:VCALENDAR')"
                    ),
                    {
                        "id": object_id, "account_id": dav_account_id,
                        "collection_id": calendar_id,
                    },
                )
                await session.commit()
            return object_id

        object_id = client.portal.call(_seed_received_invitation, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            updated = client.patch(
                f"/calendar/events/{object_id}",
                json={"summary": "My own note on this", "scope": "all"},
            )
        assert updated.status_code == 200, updated.text
        assert updated.json()["summary"] == "My own note on this"
        assert updated.json()["sequence"] == 0

    def test_update_with_attendees_sends_a_request(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """Row 111: create notifies, delete notifies -- an edit that
        moves the time has to as well, or an attendee's calendar is
        simply wrong with nothing telling them."""
        calendar_id = client.portal.call(_seed, migrated_db)
        client.portal.call(_seed_identity_and_link, migrated_db, calendar_id)

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
            object_id = created.json()["object_id"]

            updated = client.patch(
                f"/calendar/events/{object_id}",
                json={"dtstart": "2026-09-10T12:00:00+00:00",
                      "dtend": "2026-09-10T13:00:00+00:00", "scope": "all"},
            )
        assert updated.status_code == 200, updated.text

        async def _count_update_requests(db: DatabaseConnection) -> int:
            async with db.session() as session:
                result = await session.execute(
                    text(
                        "SELECT count(*) FROM outbox WHERE from_addr = 'freddy@work.example' "
                        "AND subject LIKE 'Updated:%' "
                        "AND 'anna@example.com' = ANY(SELECT jsonb_array_elements_text(to_addrs))"
                    ),
                )
                return result.scalar_one()

        assert client.portal.call(_count_update_requests, migrated_db) == 1

    def test_update_without_attendees_sends_nothing(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        calendar_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/calendar/events",
                json={
                    "calendar_id": str(calendar_id), "summary": "Solo focus block",
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

        async def _outbox_count(db: DatabaseConnection) -> int:
            async with db.session() as session:
                result = await session.execute(
                    text("SELECT count(*) FROM outbox WHERE subject = 'Updated: Renamed'"),
                )
                return result.scalar_one()

        assert client.portal.call(_outbox_count, migrated_db) == 0

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

    def test_update_naming_attendees_is_refused_not_silently_dropped(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """Row 143: the endpoint reads neither attendees nor tz -- a
        caller naming either must get an error, not a 200 that reports
        success for a field it never touched."""
        calendar_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/calendar/events",
                json={
                    "calendar_id": str(calendar_id), "summary": "Standup",
                    "dtstart": "2026-09-01T09:00:00+00:00",
                    "dtend": "2026-09-01T09:15:00+00:00",
                },
            )
            object_id = created.json()["object_id"]
            before = client.get(f"/calendar/events/{object_id}")

            resp = client.patch(
                f"/calendar/events/{object_id}",
                json={"attendees": [{"email": "anna@example.com"}]},
            )
            assert resp.status_code == 422, resp.text

            after = client.get(f"/calendar/events/{object_id}")
        # Refused, not silently accepted with nothing changed underneath.
        assert after.json()["attendees"] == before.json()["attendees"] == []
        assert after.json()["sequence"] == before.json()["sequence"]

    def test_update_naming_tz_is_refused_not_silently_dropped(
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
                },
            )
            object_id = created.json()["object_id"]
            resp = client.patch(
                f"/calendar/events/{object_id}", json={"tz": "Europe/Berlin"},
            )
        assert resp.status_code == 422, resp.text

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

    def test_a_purged_outbox_row_reports_unknown_not_pending(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """An outbox row that has aged out of retention is a different
        fact from one genuinely still in flight, and has to read as one:
        "pending" is a claim that stops being true once the send lands,
        and a row that no longer exists can never revisit it."""
        _calendar_id, object_id, identity_id = client.portal.call(
            self._seed_invitation, migrated_db,
        )
        with patch(_TARGET, return_value=migrated_db):
            responded = client.post(
                f"/calendar/events/{object_id}/respond",
                json={"identity_id": str(identity_id), "partstat": "accepted"},
            )
        assert responded.status_code == 200, responded.text
        # Still in flight: the row exists, and its own live status shows.
        assert responded.json()["own_reply"]["outbox_status"] == "pending"
        outbox_id = uuid.UUID(responded.json()["own_reply"]["outbox_id"])

        async def _purge_outbox_row_and_link_identity(db: DatabaseConnection) -> None:
            # GET's own own_reply resolution reads the identity from
            # calendar_prefs (unlike respond_to_event's response, which
            # already knows it from the request) -- the link this test
            # needs to see own_reply at all on a plain GET, not just the
            # respond call's own echo of what it was just given.
            async with db.session() as session:
                await session.execute(
                    text(
                        "INSERT INTO calendar_prefs (collection_id, identity_id) "
                        "VALUES (:collection_id, :identity_id)"
                    ),
                    {"collection_id": _calendar_id, "identity_id": identity_id},
                )
                await session.execute(
                    text("DELETE FROM outbox WHERE id = :id"), {"id": outbox_id},
                )
                await session.commit()

        client.portal.call(_purge_outbox_row_and_link_identity, migrated_db)

        with patch(_TARGET, return_value=migrated_db):
            refetched = client.get(f"/calendar/events/{object_id}")
        assert refetched.status_code == 200, refetched.text
        own_reply = refetched.json()["own_reply"]
        assert own_reply is not None
        assert own_reply["outbox_status"] == "unknown"
        assert own_reply["outbox_status"] != "pending"
        assert own_reply["partstat"] == "accepted"


class TestRecurrenceRoundTrip:
    """Row 125: an object shaped like something a real CalDAV server
    produced must survive an edit through the PATCH endpoint an agent or
    the UI actually calls, not just the pure ical.py functions
    underneath it."""

    async def _seed_exotic_event(self, db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID]:
        async with db.session() as session:
            dav_account_id, collection_id = await _seed_calendar(session)
            object_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO dav_objects (id, account_id, collection_id, kind, data) "
                    "VALUES (:id, :account_id, :collection_id, 'calendar', :data)"
                ),
                {
                    "id": object_id, "account_id": dav_account_id,
                    "collection_id": collection_id, "data": _EXOTIC_RECURRING_ICS,
                },
            )
            await session.commit()
        return collection_id, object_id

    async def _raw_data(self, db: DatabaseConnection, object_id: uuid.UUID) -> str:
        async with db.session() as session:
            result = await session.execute(
                text("SELECT data FROM dav_objects WHERE id = :id"), {"id": object_id},
            )
            return str(result.scalar_one())

    def test_editing_the_summary_through_the_api_preserves_everything_else(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        _calendar_id, object_id = client.portal.call(self._seed_exotic_event, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            resp = client.patch(
                f"/calendar/events/{object_id}",
                json={"summary": "Renamed via API", "scope": "all"},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["summary"] == "Renamed via API"

        raw = client.portal.call(self._raw_data, migrated_db, object_id)
        assert "BEGIN:VTIMEZONE" in raw
        assert "TZID:Europe/Berlin" in raw
        assert "EXDATE" in raw
        assert "RDATE" in raw
        assert "X-CUSTOM-CLIENT-ID:abc-123" in raw
        assert "CATEGORIES:WORK,PLANNING" in raw
        assert "BEGIN:VALARM" in raw
        assert "Weekly standup (moved)" in raw  # the exception, untouched

        master, exceptions = ical.parse_master_and_exceptions(raw)
        assert master.summary == "Renamed via API"
        assert len(exceptions) == 1

        # Excluding the EXDATE date and honouring the moved exception and
        # the RDATE addition still holds after an edit through the API.
        instances = ical.expand_instances(
            raw,
            datetime(2026, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 11, 1, tzinfo=timezone.utc),
        )
        # 6 RRULE occurrences, one excluded by EXDATE, plus RDATE's addition.
        assert len(instances) == 6
        assert not any(i.dtstart.day == 14 and i.dtstart.month == 9 for i in instances)

    def test_editing_one_occurrence_leaves_the_master_and_its_recurrence_data_alone(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        _calendar_id, object_id = client.portal.call(self._seed_exotic_event, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            resp = client.patch(
                f"/calendar/events/{object_id}",
                json={
                    "summary": "Moved once more", "scope": "this",
                    "recurrence_id": "20260921T090000",
                },
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["summary"] == "Moved once more"

        raw = client.portal.call(self._raw_data, migrated_db, object_id)
        master, exceptions = ical.parse_master_and_exceptions(raw)
        assert master.summary == "Weekly standup"
        assert "BEGIN:VTIMEZONE" in raw
        assert "EXDATE" in raw
        assert "RDATE" in raw
        assert len(exceptions) == 1
        assert exceptions[0].summary == "Moved once more"


class TestCreateWithComplexRrule:
    """Row 125's other half: creating complex recurrence through the API
    -- an interval and an end -- has to actually work, not just survive
    what already exists."""

    def test_create_with_interval_and_until(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        calendar_id = client.portal.call(_seed, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            created = client.post(
                "/calendar/events",
                json={
                    "calendar_id": str(calendar_id), "summary": "Fortnightly review",
                    "dtstart": "2026-09-07T10:00:00+00:00",
                    "dtend": "2026-09-07T11:00:00+00:00",
                    "rrule": "FREQ=WEEKLY;INTERVAL=2;UNTIL=20261102T100000Z",
                },
            )
            assert created.status_code == 201, created.text

            listed = client.get(
                "/calendar/events", params={"month": "2026-10", "calendars": str(calendar_id)},
            )
        assert listed.status_code == 200
        starts = sorted(
            datetime.fromisoformat(e["dtstart"])
            for e in listed.json()["events"] if e["summary"] == "Fortnightly review"
        )
        # Every 2 weeks from 2026-09-07T10:00Z: 09-21, 10-05, 10-19, 11-02.
        # Only 10-05 and 10-19 fall inside October's [start, end) window;
        # 11-02 sits on UNTIL's own boundary but the following month.
        assert starts == [
            datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc),
        ]
