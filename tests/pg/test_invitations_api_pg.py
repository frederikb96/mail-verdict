"""
GET/POST /api/calendar/invitations, against a real database and a real
PostIMAP. No live DAV server backs these tests -- see
test_calendar_intake_pg.py's module docstring for why a dav_objects row
needs `uid` seeded explicitly rather than left for a real sync engine.
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

from mail_verdict.api.invitations import router as invitations_router
from mail_verdict.calendar import ical
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Identity

_TARGET = "mail_verdict.api.invitations.get_db_connection"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(invitations_router)
    with TestClient(app) as c:
        yield c


def _new_uid() -> str:
    return f"invite-{uuid.uuid4().hex}@example.com"


def _request_ics(uid: str, *, attendee: str = "freddy@work.example") -> str:
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Test//EN\r\n"
        "METHOD:REQUEST\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        "DTSTAMP:20260901T120000Z\r\n"
        "DTSTART:20260910T090000Z\r\n"
        "DTEND:20260910T100000Z\r\n"
        "SUMMARY:Kickoff\r\n"
        "SEQUENCE:0\r\n"
        "ORGANIZER;CN=Anna Mueller:mailto:anna@example.com\r\n"
        f"ATTENDEE;CN=Freddy;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;"
        f"RSVP=TRUE:mailto:{attendee}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


def _cancel_ics(uid: str, *, sequence: int = 1) -> str:
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Test//EN\r\n"
        "METHOD:CANCEL\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        "DTSTAMP:20260902T120000Z\r\n"
        "DTSTART:20260910T090000Z\r\n"
        "DTEND:20260910T100000Z\r\n"
        "SUMMARY:Kickoff\r\n"
        f"SEQUENCE:{sequence}\r\n"
        "ORGANIZER;CN=Anna Mueller:mailto:anna@example.com\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


async def _seed_mail_account_folder_and_identity(
    session: AsyncSession, *, email: str | None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID | None]:
    account_id, folder_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text("INSERT INTO folders (id, account_id, imap_name) VALUES (:id, :account_id, 'INBOX')"),
        {"id": folder_id, "account_id": account_id},
    )
    identity_id = None
    if email is not None:
        identity_id = uuid.uuid4()
        session.add(Identity(id=identity_id, account_id=account_id, email=email))
        await session.flush()
    return account_id, folder_id, identity_id


async def _seed_dav_calendar(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    dav_account_id, collection_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dav_accounts (id, name, url, username, password) "
            "VALUES (:id, :name, 'https://dav.example.com/', 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": dav_account_id, "name": f"dav-{dav_account_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO dav_collections (id, account_id, kind, slug, display_name) "
            "VALUES (:id, :account_id, 'calendar', 'work', 'Work')"
        ),
        {"id": collection_id, "account_id": dav_account_id},
    )
    return dav_account_id, collection_id


async def _seed_message_with_ics(
    session: AsyncSession, *, account_id: uuid.UUID, folder_id: uuid.UUID, data: str,
) -> uuid.UUID:
    message_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
            " from_addr, received_at, size_bytes) "
            "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :msg_id, 'Kickoff', "
            " 'anna@example.com', now(), 1024)"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": folder_id,
            "thread_id": message_id, "msg_id": f"<{message_id}@example.com>",
        },
    )
    await session.execute(
        text(
            "INSERT INTO attachments (id, message_id, filename, content_type, data) "
            "VALUES (:id, :message_id, 'invite.ics', 'text/calendar', :data)"
        ),
        {"id": uuid.uuid4(), "message_id": message_id, "data": data.encode("utf-8")},
    )
    return message_id


class TestGetInvitation:
    def test_never_processed_message_with_intake_calendar_reads_as_unlinked(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """No calendar_intake row exists (the listener never ran on this
        message) -- decide()'s "imported" is what auto-import WOULD do,
        not something already true, so GET must not claim it happened."""
        uid = _new_uid()

        async def _seed(db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID]:
            async with db.session() as session:
                account_id, folder_id, identity_id = (
                    await _seed_mail_account_folder_and_identity(
                        session, email="freddy@work.example",
                    )
                )
                assert identity_id is not None
                _dav_account_id, collection_id = await _seed_dav_calendar(session)
                await session.execute(
                    text(
                        "INSERT INTO calendar_prefs (collection_id, identity_id, intake) "
                        "VALUES (:c, :i, true)"
                    ),
                    {"c": collection_id, "i": identity_id},
                )
                mail_id = await _seed_message_with_ics(
                    session, account_id=account_id, folder_id=folder_id, data=_request_ics(uid),
                )
                await session.commit()
            return mail_id, collection_id

        mail_id, _collection_id = client.portal.call(_seed, migrated_db)

        with patch(_TARGET, return_value=migrated_db):
            resp = client.get(f"/calendar/invitations/{mail_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "unlinked"
        assert body["object_id"] is None
        assert body["summary"] == "Kickoff"
        assert body["own_address"] == "freddy@work.example"

    def test_message_with_no_calendar_attachment_is_404(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        async def _seed(db: DatabaseConnection) -> uuid.UUID:
            async with db.session() as session:
                account_id, folder_id, _identity_id = (
                    await _seed_mail_account_folder_and_identity(session, email=None)
                )
                message_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO messages "
                        "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject) "
                        "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :msg_id, 'Hi')"
                    ),
                    {
                        "id": message_id, "account_id": account_id, "folder_id": folder_id,
                        "thread_id": message_id, "msg_id": f"<{message_id}@example.com>",
                    },
                )
                await session.commit()
            return message_id

        message_id = client.portal.call(_seed, migrated_db)

        with patch(_TARGET, return_value=migrated_db):
            resp = client.get(f"/calendar/invitations/{message_id}")
        assert resp.status_code == 404

    def test_existing_object_reads_its_stored_state_not_a_fresh_decision(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """A message the listener already processed reads back the
        calendar_intake row it left -- not decide() run again, which
        matters once a linked calendar changes after the fact."""
        uid = _new_uid()

        async def _seed(db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
            async with db.session() as session:
                account_id, folder_id, identity_id = (
                    await _seed_mail_account_folder_and_identity(
                        session, email="freddy@work.example",
                    )
                )
                assert identity_id is not None
                dav_account_id, collection_id = await _seed_dav_calendar(session)
                mail_id = await _seed_message_with_ics(
                    session, account_id=account_id, folder_id=folder_id, data=_request_ics(uid),
                )
                object_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO dav_objects (id, account_id, collection_id, kind, data, uid) "
                        "VALUES (:id, :account_id, :collection_id, 'calendar', :data, :uid)"
                    ),
                    {
                        "id": object_id, "account_id": dav_account_id,
                        "collection_id": collection_id,
                        "data": ical.strip_method(_request_ics(uid)), "uid": uid,
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO calendar_intake "
                        "(account_id, msg_key, ical_uid, method, sequence, dav_account_id, "
                        " collection_id, object_id, status) "
                        "VALUES (:account_id, :msg_key, :uid, 'REQUEST', 0, :dav_account_id, "
                        " :collection_id, :object_id, 'imported')"
                    ),
                    {
                        "account_id": account_id, "msg_key": f"<{mail_id}@example.com>",
                        "uid": uid, "dav_account_id": dav_account_id,
                        "collection_id": collection_id, "object_id": object_id,
                    },
                )
                await session.commit()
            return mail_id, collection_id, object_id

        mail_id, collection_id, object_id = client.portal.call(_seed, migrated_db)

        with patch(_TARGET, return_value=migrated_db):
            resp = client.get(f"/calendar/invitations/{mail_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "imported"
        assert body["calendar_id"] == str(collection_id)
        assert body["object_id"] == str(object_id)

    def test_request_against_a_known_uid_is_pending_review_with_from_addr(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """Row 107 v2: a REQUEST naming a UID already held is never
        applied automatically -- the card shows pending_review with the
        message's own envelope sender next to the ORGANIZER it claims,
        so a mismatch is obvious without any header authentication."""
        uid = _new_uid()

        async def _seed(db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
            async with db.session() as session:
                account_id, folder_id, _identity_id = await _seed_mail_account_folder_and_identity(
                    session, email=None,
                )
                dav_account_id, collection_id = await _seed_dav_calendar(session)
                mail_id = await _seed_message_with_ics(
                    session, account_id=account_id, folder_id=folder_id, data=_request_ics(uid),
                )
                object_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO dav_objects (id, account_id, collection_id, kind, data, uid) "
                        "VALUES (:id, :account_id, :collection_id, 'calendar', :data, :uid)"
                    ),
                    {
                        "id": object_id, "account_id": dav_account_id,
                        "collection_id": collection_id,
                        "data": ical.strip_method(_request_ics(uid)), "uid": uid,
                    },
                )
                await session.commit()
            return mail_id, collection_id, object_id

        mail_id, collection_id, object_id = client.portal.call(_seed, migrated_db)

        with patch(_TARGET, return_value=migrated_db):
            resp = client.get(f"/calendar/invitations/{mail_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "pending_review"
        assert body["calendar_id"] == str(collection_id)
        assert body["object_id"] == str(object_id)
        assert body["from_addr"] == "anna@example.com"
        assert body["organizer"]["email"] == "anna@example.com"


class TestImportInvitation:
    def test_import_into_an_unlinked_invitation_creates_the_event(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        uid = _new_uid()

        async def _seed(db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID]:
            async with db.session() as session:
                account_id, folder_id, _identity_id = (
                    await _seed_mail_account_folder_and_identity(session, email=None)
                )
                _dav_account_id, collection_id = await _seed_dav_calendar(session)
                mail_id = await _seed_message_with_ics(
                    session, account_id=account_id, folder_id=folder_id, data=_request_ics(uid),
                )
                await session.commit()
            return mail_id, collection_id

        mail_id, collection_id = client.portal.call(_seed, migrated_db)

        with patch(_TARGET, return_value=migrated_db):
            get_first = client.get(f"/calendar/invitations/{mail_id}")
            assert get_first.json()["status"] == "unlinked"

            resp = client.post(
                f"/calendar/invitations/{mail_id}/import",
                json={"calendar_id": str(collection_id)},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "imported"
        assert body["calendar_id"] == str(collection_id)
        assert body["object_id"] is not None

    def test_import_with_link_sets_the_identity_as_intake(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        uid = _new_uid()

        async def _seed(db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
            async with db.session() as session:
                account_id, folder_id, identity_id = (
                    await _seed_mail_account_folder_and_identity(
                        session, email="freddy@work.example",
                    )
                )
                assert identity_id is not None
                _dav_account_id, collection_id = await _seed_dav_calendar(session)
                mail_id = await _seed_message_with_ics(
                    session, account_id=account_id, folder_id=folder_id, data=_request_ics(uid),
                )
                await session.commit()
            return mail_id, collection_id, identity_id

        mail_id, collection_id, identity_id = client.portal.call(_seed, migrated_db)

        with patch(_TARGET, return_value=migrated_db):
            resp = client.post(
                f"/calendar/invitations/{mail_id}/import",
                json={"calendar_id": str(collection_id), "link": True},
            )
        assert resp.status_code == 200, resp.text

        async def _read_prefs(db: DatabaseConnection) -> tuple[uuid.UUID | None, bool]:
            async with db.session() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT identity_id, intake FROM calendar_prefs "
                            "WHERE collection_id = :c"
                        ),
                        {"c": collection_id},
                    )
                ).one()
                return row.identity_id, row.intake

        linked_identity_id, is_intake = client.portal.call(_read_prefs, migrated_db)
        assert linked_identity_id == identity_id
        assert is_intake is True

    def test_import_with_link_and_no_attendee_identity_is_422(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """link=true has to know which identity to link -- a forwarded
        invitation (no identity among the attendees) has none."""
        uid = _new_uid()

        async def _seed(db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID]:
            async with db.session() as session:
                account_id, folder_id, _identity_id = (
                    await _seed_mail_account_folder_and_identity(session, email=None)
                )
                _dav_account_id, collection_id = await _seed_dav_calendar(session)
                mail_id = await _seed_message_with_ics(
                    session, account_id=account_id, folder_id=folder_id,
                    data=_request_ics(uid, attendee="someone-else@example.com"),
                )
                await session.commit()
            return mail_id, collection_id

        mail_id, collection_id = client.portal.call(_seed, migrated_db)

        with patch(_TARGET, return_value=migrated_db):
            resp = client.post(
                f"/calendar/invitations/{mail_id}/import",
                json={"calendar_id": str(collection_id), "link": True},
            )
        assert resp.status_code == 422

    def test_retry_after_a_failed_write_creates_a_fresh_object(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """A dead-lettered create tombstones the object it was for (see
        the consumer contract's "Pending writes and conflicts") -- import
        called again with the same calendar_id must not find that
        tombstoned row and must produce a working one."""
        uid = _new_uid()

        async def _seed(db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID]:
            async with db.session() as session:
                account_id, folder_id, _identity_id = (
                    await _seed_mail_account_folder_and_identity(session, email=None)
                )
                dav_account_id, collection_id = await _seed_dav_calendar(session)
                mail_id = await _seed_message_with_ics(
                    session, account_id=account_id, folder_id=folder_id, data=_request_ics(uid),
                )
                dead_object_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO dav_objects "
                        "(id, account_id, collection_id, kind, data, uid, deleted_at) "
                        "VALUES (:id, :account_id, :collection_id, 'calendar', :data, :uid, now())"
                    ),
                    {
                        "id": dead_object_id, "account_id": dav_account_id,
                        "collection_id": collection_id,
                        "data": ical.strip_method(_request_ics(uid)), "uid": uid,
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO calendar_intake "
                        "(account_id, msg_key, ical_uid, method, sequence, dav_account_id, "
                        " collection_id, object_id, status) "
                        "VALUES (:account_id, :msg_key, :uid, 'REQUEST', 0, :dav_account_id, "
                        " :collection_id, :object_id, 'imported')"
                    ),
                    {
                        "account_id": account_id, "msg_key": f"<{mail_id}@example.com>",
                        "uid": uid, "dav_account_id": dav_account_id,
                        "collection_id": collection_id, "object_id": dead_object_id,
                    },
                )
                await session.commit()
            return mail_id, collection_id

        mail_id, collection_id = client.portal.call(_seed, migrated_db)

        with patch(_TARGET, return_value=migrated_db):
            resp = client.post(
                f"/calendar/invitations/{mail_id}/import",
                json={"calendar_id": str(collection_id)},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "imported"
        assert body["object_id"] is not None

        async def _live_object_count(db: DatabaseConnection) -> int:
            async with db.session() as session:
                result = await session.execute(
                    text(
                        "SELECT count(*) FROM dav_objects "
                        "WHERE collection_id = :c AND deleted_at IS NULL"
                    ),
                    {"c": collection_id},
                )
                return int(result.scalar_one())

        assert client.portal.call(_live_object_count, migrated_db) == 1

    def test_import_of_an_existing_uid_updates_in_place(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """The dropdown's chosen calendar is not used when the UID
        already resolves somewhere -- the hand-imported case, updated in
        place rather than duplicated."""
        uid = _new_uid()

        async def _seed(
            db: DatabaseConnection,
        ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
            async with db.session() as session:
                account_id, folder_id, _identity_id = (
                    await _seed_mail_account_folder_and_identity(session, email=None)
                )
                dav_account_id, existing_collection_id = await _seed_dav_calendar(session)
                _other_dav_account_id, other_collection_id = await _seed_dav_calendar(session)
                mail_id = await _seed_message_with_ics(
                    session, account_id=account_id, folder_id=folder_id, data=_request_ics(uid),
                )
                object_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO dav_objects (id, account_id, collection_id, kind, data, uid) "
                        "VALUES (:id, :account_id, :collection_id, 'calendar', :data, :uid)"
                    ),
                    {
                        "id": object_id, "account_id": dav_account_id,
                        "collection_id": existing_collection_id,
                        "data": ical.strip_method(_request_ics(uid)), "uid": uid,
                    },
                )
                await session.commit()
            return mail_id, existing_collection_id, other_collection_id, object_id

        mail_id, existing_collection_id, other_collection_id, object_id = client.portal.call(
            _seed, migrated_db,
        )

        with patch(_TARGET, return_value=migrated_db):
            resp = client.post(
                f"/calendar/invitations/{mail_id}/import",
                json={"calendar_id": str(other_collection_id)},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "updated"
        assert body["object_id"] == str(object_id)
        assert body["calendar_id"] == str(existing_collection_id)

        async def _other_collection_count(db: DatabaseConnection) -> int:
            async with db.session() as session:
                result = await session.execute(
                    text("SELECT count(*) FROM dav_objects WHERE collection_id = :c"),
                    {"c": other_collection_id},
                )
                return int(result.scalar_one())

        assert client.portal.call(_other_collection_count, migrated_db) == 0

    async def _seed_existing_object_and_message(
        self, db: DatabaseConnection, data: str, uid: str,
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        async with db.session() as session:
            account_id, folder_id, _identity_id = await _seed_mail_account_folder_and_identity(
                session, email=None,
            )
            dav_account_id, collection_id = await _seed_dav_calendar(session)
            mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id, data=data,
            )
            object_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO dav_objects (id, account_id, collection_id, kind, data, uid) "
                    "VALUES (:id, :account_id, :collection_id, 'calendar', :data, :uid)"
                ),
                {
                    "id": object_id, "account_id": dav_account_id, "collection_id": collection_id,
                    "data": ical.strip_method(_request_ics(uid)), "uid": uid,
                },
            )
            await session.commit()
        return mail_id, collection_id, object_id

    def test_confirming_a_pending_review_request_updates_the_object(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """Row 107 v2: confirming a pending_review REQUEST needs no
        calendar_id -- the target is the existing object itself."""
        uid = _new_uid()
        mail_id, collection_id, object_id = client.portal.call(
            self._seed_existing_object_and_message, migrated_db,
            _request_ics(uid), uid,
        )

        with patch(_TARGET, return_value=migrated_db):
            resp = client.post(f"/calendar/invitations/{mail_id}/import", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "updated"
        assert body["object_id"] == str(object_id)
        assert body["calendar_id"] == str(collection_id)

    def test_confirming_a_pending_review_cancel_marks_it_cancelled(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        uid = _new_uid()
        mail_id, collection_id, object_id = client.portal.call(
            self._seed_existing_object_and_message, migrated_db,
            _cancel_ics(uid), uid,
        )

        with patch(_TARGET, return_value=migrated_db):
            resp = client.post(f"/calendar/invitations/{mail_id}/import", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "cancelled"
        assert body["object_id"] == str(object_id)
        assert body["calendar_id"] == str(collection_id)

        async def _stored_status(db: DatabaseConnection) -> str:
            async with db.session() as session:
                data = (
                    await session.execute(
                        text("SELECT data FROM dav_objects WHERE id = :id"), {"id": object_id},
                    )
                ).scalar_one()
            master, _ = ical.parse_master_and_exceptions(data)
            return master.status

        assert client.portal.call(_stored_status, migrated_db) == "cancelled"

    def test_cancel_of_an_unknown_uid_via_import_is_404(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        uid = _new_uid()

        async def _seed(db: DatabaseConnection) -> uuid.UUID:
            async with db.session() as session:
                account_id, folder_id, _identity_id = (
                    await _seed_mail_account_folder_and_identity(session, email=None)
                )
                mail_id = await _seed_message_with_ics(
                    session, account_id=account_id, folder_id=folder_id, data=_cancel_ics(uid),
                )
                await session.commit()
            return mail_id

        mail_id = client.portal.call(_seed, migrated_db)

        with patch(_TARGET, return_value=migrated_db):
            resp = client.post(f"/calendar/invitations/{mail_id}/import", json={})
        assert resp.status_code == 404

    def test_new_invitation_without_calendar_id_is_400(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        uid = _new_uid()

        async def _seed(db: DatabaseConnection) -> uuid.UUID:
            async with db.session() as session:
                account_id, folder_id, _identity_id = (
                    await _seed_mail_account_folder_and_identity(session, email=None)
                )
                mail_id = await _seed_message_with_ics(
                    session, account_id=account_id, folder_id=folder_id, data=_request_ics(uid),
                )
                await session.commit()
            return mail_id

        mail_id = client.portal.call(_seed, migrated_db)

        with patch(_TARGET, return_value=migrated_db):
            resp = client.post(f"/calendar/invitations/{mail_id}/import", json={})
        assert resp.status_code == 400

    def test_get_describes_what_post_will_actually_do_for_an_unreachable_uid(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """Row 120: GET's own existing-object lookup is scoped the way
        the automatic listener is (reachable DAV accounts only), but
        POST .../import resolves a UID anywhere -- without accounting
        for that, the card could say 'unlinked, pick a calendar' for an
        invitation whose UID actually collides with an object elsewhere,
        and POST would then silently update that object instead of
        creating one in the calendar the person just chose."""
        uid = _new_uid()

        async def _seed(db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
            async with db.session() as session:
                # No identity, no calendar_prefs link at all -- the
                # object lives under a DAV account this mail account has
                # no reachable link to.
                account_id, folder_id, _identity_id = (
                    await _seed_mail_account_folder_and_identity(session, email=None)
                )
                dav_account_id, collection_id = await _seed_dav_calendar(session)
                mail_id = await _seed_message_with_ics(
                    session, account_id=account_id, folder_id=folder_id, data=_request_ics(uid),
                )
                object_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO dav_objects (id, account_id, collection_id, kind, data, uid) "
                        "VALUES (:id, :account_id, :collection_id, 'calendar', :data, :uid)"
                    ),
                    {
                        "id": object_id, "account_id": dav_account_id,
                        "collection_id": collection_id,
                        "data": ical.strip_method(_request_ics(uid)), "uid": uid,
                    },
                )
                await session.commit()
            return mail_id, collection_id, object_id

        mail_id, collection_id, object_id = client.portal.call(_seed, migrated_db)

        with patch(_TARGET, return_value=migrated_db):
            resp = client.get(f"/calendar/invitations/{mail_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Not "unlinked" -- GET describes what POST would actually do,
        # which is confirm a change against the object it already found.
        assert body["status"] == "pending_review"
        assert body["object_id"] == str(object_id)
        assert body["calendar_id"] == str(collection_id)
