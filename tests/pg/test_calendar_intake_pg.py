"""
calendar/intake.py against a real database and a real PostIMAP -- turning
an emailed .ics attachment into a calendar entry, and the invariants the
design settles: never-twice, stale SEQUENCE ignored, a known UID updated
wherever it lives rather than duplicated, a cancellation staying visible.

No live DAV server backs these tests (the same choice
test_contacts_api_pg.py and test_calendar_events_api_pg.py made): a
dav_objects row's parsed `uid` column is filled in by PostIMAP's own
outbound sync against a real server, which nothing here runs, so a row
this suite creates through create_object() never becomes findable by
find_by_uid_anywhere(). Wherever a test needs an "already imported"
object to exist, it seeds one directly with `uid` set, the same pattern
those two files use for the parsed columns they need read back --
proving intake's own query and decision logic, not that a real server
round-trip fills in `uid` the way it does in production.
"""

from __future__ import annotations

import itertools
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.calendar import ical
from mail_verdict.calendar.intake import CalendarIntakeHandler
from mail_verdict.calendar.repository import (
    CalendarIntakeRepository,
    CalendarPrefsRepository,
    CollectionRepository,
    DavAccountRepository,
    DavObjectRepository,
)
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Identity
from mail_verdict.database.repository import MessageRepository
from mail_verdict.postimap.listener import PostimapEvent

_imap_uid_counter = itertools.count(1)


def _new_uid() -> str:
    """A fresh iCalendar UID -- pg tests share one database for the whole
    session, so a fixed UID across test functions would let a later test
    find an earlier test's dav_objects row through find_by_uid_anywhere()."""
    return f"invite-{uuid.uuid4().hex}@example.com"


def _request_ics(uid: str, *, sequence: int = 0, attendee: str = "freddy@work.example") -> str:
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
        f"SEQUENCE:{sequence}\r\n"
        "ORGANIZER;CN=Anna Mueller:mailto:anna@example.com\r\n"
        f"ATTENDEE;CN=Freddy;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;"
        f"RSVP=TRUE:mailto:{attendee}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


def _stored_event_data(uid: str, *, sequence: int = 0) -> str:
    """What create_object() would have written for a REQUEST already
    imported -- METHOD stripped, as calendar/intake.py's own import path
    stores it."""
    return ical.strip_method(_request_ics(uid, sequence=sequence))


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


def _reply_ics(
    uid: str, *, attendee: str = "freddy@work.example", partstat: str = "ACCEPTED",
) -> str:
    """No DTSTART -- RFC 5546 does not require it on a REPLY, and
    parse_itip_message() is what tolerates that."""
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Test//EN\r\n"
        "METHOD:REPLY\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        "DTSTAMP:20260903T120000Z\r\n"
        "SEQUENCE:0\r\n"
        "ORGANIZER;CN=Anna Mueller:mailto:anna@example.com\r\n"
        f"ATTENDEE;PARTSTAT={partstat}:mailto:{attendee}\r\n"
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


async def _seed_dav_calendar(
    session: AsyncSession, *, is_active: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    dav_account_id, collection_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dav_accounts (id, name, url, username, password, is_active) "
            "VALUES (:id, :name, 'https://dav.example.com/', 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'), :is_active)"
        ),
        {"id": dav_account_id, "name": f"dav-{dav_account_id}", "is_active": is_active},
    )
    await session.execute(
        text(
            "INSERT INTO dav_collections (id, account_id, kind, slug, display_name) "
            "VALUES (:id, :account_id, 'calendar', 'work', 'Work')"
        ),
        {"id": collection_id, "account_id": dav_account_id},
    )
    return dav_account_id, collection_id


async def _link_intake_calendar(
    session: AsyncSession, *, collection_id: uuid.UUID, identity_id: uuid.UUID,
) -> None:
    await session.execute(
        text(
            "INSERT INTO calendar_prefs (collection_id, identity_id, intake) "
            "VALUES (:collection_id, :identity_id, true)"
        ),
        {"collection_id": collection_id, "identity_id": identity_id},
    )


async def _seed_existing_object(
    session: AsyncSession, *, dav_account_id: uuid.UUID, collection_id: uuid.UUID,
    uid: str, data: str,
) -> uuid.UUID:
    """A dav_objects row standing in for one already imported and synced
    -- see the module docstring for why `uid` has to be set explicitly
    here rather than left for a real sync engine to fill in."""
    object_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dav_objects (id, account_id, collection_id, kind, data, uid) "
            "VALUES (:id, :account_id, :collection_id, 'calendar', :data, :uid)"
        ),
        {
            "id": object_id, "account_id": dav_account_id,
            "collection_id": collection_id, "data": data, "uid": uid,
        },
    )
    return object_id


async def _seed_message_with_ics(
    session: AsyncSession, *, account_id: uuid.UUID, folder_id: uuid.UUID, data: str,
    message_id_hdr: str,
) -> uuid.UUID:
    message_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
            " from_addr, received_at, size_bytes) "
            "VALUES (:id, :account_id, :folder_id, :imap_uid, :thread_id, :msg_id, 'Kickoff', "
            " 'anna@example.com', now(), 1024)"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": folder_id,
            "imap_uid": next(_imap_uid_counter),
            "thread_id": message_id, "msg_id": message_id_hdr,
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


def _handler(db: DatabaseConnection) -> CalendarIntakeHandler:
    return CalendarIntakeHandler(
        db, CalendarIntakeRepository(db), DavObjectRepository(db),
        CalendarPrefsRepository(db), CollectionRepository(db), DavAccountRepository(db),
    )


def _insert_event(mail_id: uuid.UUID, account_id: uuid.UUID) -> PostimapEvent:
    return PostimapEvent(
        v=1, type="message", op="insert", id=str(mail_id), account_id=str(account_id),
        origin="sync",
    )


class TestImport:
    @pytest.mark.asyncio
    async def test_request_with_no_intake_calendar_is_unlinked(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        uid = _new_uid()
        async with migrated_db.session() as session:
            account_id, folder_id, identity_id = await _seed_mail_account_folder_and_identity(
                session, email="freddy@work.example",
            )
            mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_request_ics(uid), message_id_hdr="<m1@example.com>",
            )
        assert identity_id is not None

        await _handler(migrated_db).handle_message_event(_insert_event(mail_id, account_id))

        async with migrated_db.session() as session:
            row = (
                await session.execute(
                    text("SELECT status, object_id FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).one()
        assert row.status == "unlinked"
        assert row.object_id is None

    @pytest.mark.asyncio
    async def test_request_with_intake_calendar_is_imported(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        uid = _new_uid()
        async with migrated_db.session() as session:
            account_id, folder_id, identity_id = await _seed_mail_account_folder_and_identity(
                session, email="freddy@work.example",
            )
            assert identity_id is not None
            _dav_account_id, collection_id = await _seed_dav_calendar(session)
            await _link_intake_calendar(
                session, collection_id=collection_id, identity_id=identity_id,
            )
            mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_request_ics(uid), message_id_hdr="<m2@example.com>",
            )

        await _handler(migrated_db).handle_message_event(_insert_event(mail_id, account_id))

        async with migrated_db.session() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status, object_id, collection_id FROM calendar_intake "
                        "WHERE account_id = :a"
                    ),
                    {"a": account_id},
                )
            ).one()
            stored = (
                await session.execute(
                    text("SELECT data FROM dav_objects WHERE id = :id"), {"id": row.object_id},
                )
            ).scalar_one()
        assert row.status == "imported"
        assert row.object_id is not None
        assert row.collection_id == collection_id
        assert "METHOD" not in stored
        assert "SCHEDULE-AGENT=CLIENT" in ical.set_schedule_agent_client_on_organizer(stored)
        master, _ = ical.parse_master_and_exceptions(stored)
        assert master.uid == uid

    @pytest.mark.asyncio
    async def test_redelivered_message_is_a_no_op(self, migrated_db: DatabaseConnection) -> None:
        """The never-classify-twice gate's calendar counterpart: the same
        message/insert event handled twice (a resync) must not produce a
        second dav_objects row -- even though decide() itself would
        compute "imported" again on the second call in this test
        environment (find_by_uid_anywhere never finds the first call's
        row here, see the module docstring), the msg_key gate in
        _write_intake_row() is what actually prevents it."""
        uid = _new_uid()
        async with migrated_db.session() as session:
            account_id, folder_id, identity_id = await _seed_mail_account_folder_and_identity(
                session, email="freddy@work.example",
            )
            assert identity_id is not None
            _dav_account_id, collection_id = await _seed_dav_calendar(session)
            await _link_intake_calendar(
                session, collection_id=collection_id, identity_id=identity_id,
            )
            mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_request_ics(uid), message_id_hdr="<m3@example.com>",
            )

        handler = _handler(migrated_db)
        event = _insert_event(mail_id, account_id)
        await handler.handle_message_event(event)
        await handler.handle_message_event(event)

        async with migrated_db.session() as session:
            object_count = (
                await session.execute(
                    text("SELECT count(*) FROM dav_objects WHERE collection_id = :c"),
                    {"c": collection_id},
                )
            ).scalar_one()
            intake_count = (
                await session.execute(
                    text("SELECT count(*) FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).scalar_one()
        assert object_count == 1
        assert intake_count == 1

    @pytest.mark.asyncio
    async def test_two_identities_invited_to_the_same_event_never_duplicate(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The second of Freddy's own addresses invited to the same
        event is an in-place update, resolved by UID -- never a second
        copy. The first identity's own invitation already imported is
        represented by a pre-seeded object (see the module docstring)."""
        uid = _new_uid()
        async with migrated_db.session() as session:
            account_id, folder_id, identity_id = await _seed_mail_account_folder_and_identity(
                session, email="freddy@work.example",
            )
            assert identity_id is not None
            second_identity_id = uuid.uuid4()
            session.add(
                Identity(
                    id=second_identity_id, account_id=account_id,
                    email="f.berg@personal.example",
                ),
            )
            dav_account_id, collection_id = await _seed_dav_calendar(session)
            await _link_intake_calendar(
                session, collection_id=collection_id, identity_id=identity_id,
            )
            await _seed_existing_object(
                session, dav_account_id=dav_account_id, collection_id=collection_id,
                uid=uid, data=_stored_event_data(uid),
            )
            second_mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_request_ics(uid, attendee="f.berg@personal.example"),
                message_id_hdr="<m4b@example.com>",
            )

        await _handler(migrated_db).handle_message_event(_insert_event(second_mail_id, account_id))

        async with migrated_db.session() as session:
            object_count = (
                await session.execute(
                    text("SELECT count(*) FROM dav_objects WHERE collection_id = :c"),
                    {"c": collection_id},
                )
            ).scalar_one()
            status = (
                await session.execute(
                    text("SELECT status FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).scalar_one()
        assert object_count == 1
        assert status == "updated"

    @pytest.mark.asyncio
    async def test_stale_sequence_is_ignored(self, migrated_db: DatabaseConnection) -> None:
        uid = _new_uid()
        async with migrated_db.session() as session:
            account_id, folder_id, identity_id = await _seed_mail_account_folder_and_identity(
                session, email="freddy@work.example",
            )
            assert identity_id is not None
            dav_account_id, collection_id = await _seed_dav_calendar(session)
            await _link_intake_calendar(
                session, collection_id=collection_id, identity_id=identity_id,
            )
            await _seed_existing_object(
                session, dav_account_id=dav_account_id, collection_id=collection_id,
                uid=uid, data=_stored_event_data(uid, sequence=2),
            )
            stale_mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_request_ics(uid, sequence=1), message_id_hdr="<m5b@example.com>",
            )

        await _handler(migrated_db).handle_message_event(_insert_event(stale_mail_id, account_id))

        async with migrated_db.session() as session:
            status = (
                await session.execute(
                    text("SELECT status FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).scalar_one()
            stored = (
                await session.execute(
                    text("SELECT data FROM dav_objects WHERE collection_id = :c"),
                    {"c": collection_id},
                )
            ).scalar_one()
        assert status == "ignored_stale"
        master, _ = ical.parse_master_and_exceptions(stored)
        assert master.sequence == 2

    @pytest.mark.asyncio
    async def test_cancel_marks_the_event_cancelled_and_keeps_it(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        uid = _new_uid()
        async with migrated_db.session() as session:
            account_id, folder_id, identity_id = await _seed_mail_account_folder_and_identity(
                session, email="freddy@work.example",
            )
            assert identity_id is not None
            dav_account_id, collection_id = await _seed_dav_calendar(session)
            await _link_intake_calendar(
                session, collection_id=collection_id, identity_id=identity_id,
            )
            existing_object_id = await _seed_existing_object(
                session, dav_account_id=dav_account_id, collection_id=collection_id,
                uid=uid, data=_stored_event_data(uid),
            )
            cancel_mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_cancel_ics(uid), message_id_hdr="<m6b@example.com>",
            )

        await _handler(migrated_db).handle_message_event(_insert_event(cancel_mail_id, account_id))

        async with migrated_db.session() as session:
            status = (
                await session.execute(
                    text("SELECT status FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).scalar_one()
            deleted_at, stored = (
                await session.execute(
                    text("SELECT deleted_at, data FROM dav_objects WHERE id = :id"),
                    {"id": existing_object_id},
                )
            ).one()
        assert status == "cancelled"
        assert deleted_at is None  # cancelled stays visible, never removed
        master, _ = ical.parse_master_and_exceptions(stored)
        assert master.status == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_of_unknown_uid_is_ignored(self, migrated_db: DatabaseConnection) -> None:
        uid = _new_uid()
        async with migrated_db.session() as session:
            account_id, folder_id, _identity_id = await _seed_mail_account_folder_and_identity(
                session, email=None,
            )
            mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_cancel_ics(uid), message_id_hdr="<m7@example.com>",
            )

        await _handler(migrated_db).handle_message_event(_insert_event(mail_id, account_id))

        async with migrated_db.session() as session:
            status = (
                await session.execute(
                    text("SELECT status FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).scalar_one()
        assert status == "ignored"

    @pytest.mark.asyncio
    async def test_reply_updates_partstat_on_the_held_object(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        uid = _new_uid()
        async with migrated_db.session() as session:
            account_id, folder_id, identity_id = await _seed_mail_account_folder_and_identity(
                session, email="freddy@work.example",
            )
            assert identity_id is not None
            dav_account_id, collection_id = await _seed_dav_calendar(session)
            await _link_intake_calendar(
                session, collection_id=collection_id, identity_id=identity_id,
            )
            existing_object_id = await _seed_existing_object(
                session, dav_account_id=dav_account_id, collection_id=collection_id,
                uid=uid, data=_stored_event_data(uid),
            )
            reply_mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_reply_ics(uid, attendee="freddy@work.example", partstat="ACCEPTED"),
                message_id_hdr="<m8b@example.com>",
            )

        await _handler(migrated_db).handle_message_event(_insert_event(reply_mail_id, account_id))

        async with migrated_db.session() as session:
            stored = (
                await session.execute(
                    text("SELECT data FROM dav_objects WHERE id = :id"),
                    {"id": existing_object_id},
                )
            ).scalar_one()
        master, _ = ical.parse_master_and_exceptions(stored)
        attendee = next(a for a in master.attendees if a.email == "freddy@work.example")
        assert attendee.partstat == "accepted"

    @pytest.mark.asyncio
    async def test_reply_of_unknown_uid_is_ignored(self, migrated_db: DatabaseConnection) -> None:
        uid = _new_uid()
        async with migrated_db.session() as session:
            account_id, folder_id, _identity_id = await _seed_mail_account_folder_and_identity(
                session, email=None,
            )
            mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_reply_ics(uid), message_id_hdr="<m8c@example.com>",
            )

        await _handler(migrated_db).handle_message_event(_insert_event(mail_id, account_id))

        async with migrated_db.session() as session:
            status = (
                await session.execute(
                    text("SELECT status FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).scalar_one()
        assert status == "ignored"

    @pytest.mark.asyncio
    async def test_inactive_dav_account_is_unlinked(self, migrated_db: DatabaseConnection) -> None:
        uid = _new_uid()
        async with migrated_db.session() as session:
            account_id, folder_id, identity_id = await _seed_mail_account_folder_and_identity(
                session, email="freddy@work.example",
            )
            assert identity_id is not None
            _dav_account_id, collection_id = await _seed_dav_calendar(session, is_active=False)
            await _link_intake_calendar(
                session, collection_id=collection_id, identity_id=identity_id,
            )
            mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_request_ics(uid), message_id_hdr="<m9@example.com>",
            )

        await _handler(migrated_db).handle_message_event(_insert_event(mail_id, account_id))

        async with migrated_db.session() as session:
            status = (
                await session.execute(
                    text("SELECT status FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).scalar_one()
        assert status == "unlinked"

    @pytest.mark.asyncio
    async def test_backfilled_arrival_is_never_processed(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """origin != "sync" (a backfill row) is filtered before any
        parsing happens -- handle_message_event() is the module's own
        gate, matching pipeline/enqueue.py's enqueue_live_arrival."""
        uid = _new_uid()
        async with migrated_db.session() as session:
            account_id, folder_id, identity_id = await _seed_mail_account_folder_and_identity(
                session, email="freddy@work.example",
            )
            assert identity_id is not None
            _dav_account_id, collection_id = await _seed_dav_calendar(session)
            await _link_intake_calendar(
                session, collection_id=collection_id, identity_id=identity_id,
            )
            mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_request_ics(uid), message_id_hdr="<m10@example.com>",
            )

        backfill_event = PostimapEvent(
            v=1, type="message", op="insert", id=str(mail_id), account_id=str(account_id),
            origin="backfill",
        )
        await _handler(migrated_db).handle_message_event(backfill_event)

        async with migrated_db.session() as session:
            count = (
                await session.execute(
                    text("SELECT count(*) FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).scalar_one()
        assert count == 0


class TestDecideIsPure:
    @pytest.mark.asyncio
    async def test_decide_writes_nothing(self, migrated_db: DatabaseConnection) -> None:
        """decide() is what api/invitations.py's GET reuses for a message
        the listener never saw -- it must never write calendar_intake or
        dav_objects itself."""
        uid = _new_uid()
        async with migrated_db.session() as session:
            account_id, folder_id, identity_id = await _seed_mail_account_folder_and_identity(
                session, email="freddy@work.example",
            )
            assert identity_id is not None
            _dav_account_id, collection_id = await _seed_dav_calendar(session)
            await _link_intake_calendar(
                session, collection_id=collection_id, identity_id=identity_id,
            )
            mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_request_ics(uid), message_id_hdr="<m11@example.com>",
            )

        handler = _handler(migrated_db)
        message_repo = MessageRepository(migrated_db)
        message_obj = await message_repo.get_by_id(account_id, mail_id)
        assert message_obj is not None
        invitation = ical.parse_itip_message(_request_ics(uid))
        decision = await handler.decide(account_id, message_obj, invitation)
        assert decision.status == "imported"
        assert decision.collection_id == collection_id

        async with migrated_db.session() as session:
            intake_count = (
                await session.execute(
                    text("SELECT count(*) FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).scalar_one()
            object_count = (
                await session.execute(
                    text("SELECT count(*) FROM dav_objects WHERE collection_id = :c"),
                    {"c": collection_id},
                )
            ).scalar_one()
        assert intake_count == 0
        assert object_count == 0
