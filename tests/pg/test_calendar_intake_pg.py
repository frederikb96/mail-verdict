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
import json
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
from mail_verdict.database.msg_key import compute_msg_key
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


def _attacker_request_ics(uid: str, *, sequence: int = 99) -> str:
    """The row 107 repro's attack body: a REQUEST naming a UID the sender
    merely knows (a co-attendee has it, since it is in the .ics they
    themselves received), an ORGANIZER of the attacker's own choosing,
    no ATTENDEE line at all, and a SEQUENCE high enough to beat
    `_is_stale()` on its own."""
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Test//EN\r\n"
        "METHOD:REQUEST\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        "DTSTAMP:20260901T120000Z\r\n"
        "DTSTART:20260911T030000Z\r\n"
        "DTEND:20260911T040000Z\r\n"
        "SUMMARY:MOVED - see attacker.example\r\n"
        f"SEQUENCE:{sequence}\r\n"
        "ORGANIZER:mailto:attacker@evil.example\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


def _attacker_cancel_ics(uid: str, *, sequence: int = 99) -> str:
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Test//EN\r\n"
        "METHOD:CANCEL\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        "DTSTAMP:20260901T120000Z\r\n"
        "DTSTART:20260910T090000Z\r\n"
        "DTEND:20260910T100000Z\r\n"
        "SUMMARY:Kickoff\r\n"
        f"SEQUENCE:{sequence}\r\n"
        "ORGANIZER:mailto:attacker@evil.example\r\n"
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
    message_id_hdr: str, from_addr: str = "anna@example.com",
    to_addrs: list[str] | None = None, cc_addrs: list[str] | None = None,
) -> uuid.UUID:
    """from_addr defaults to the organizer used throughout this file's
    REQUEST/CANCEL fixtures -- a REPLY's own sender is the attendee it
    replies as, not the organizer, so a REPLY test passes its own."""
    message_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
            " from_addr, to_addrs, cc_addrs, received_at, size_bytes) "
            "VALUES (:id, :account_id, :folder_id, :imap_uid, :thread_id, :msg_id, 'Kickoff', "
            " :from_addr, :to_addrs, :cc_addrs, now(), 1024)"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": folder_id,
            "imap_uid": next(_imap_uid_counter),
            "thread_id": message_id, "msg_id": message_id_hdr, "from_addr": from_addr,
            "to_addrs": json.dumps(to_addrs) if to_addrs is not None else None,
            "cc_addrs": json.dumps(cc_addrs) if cc_addrs is not None else None,
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
    async def test_addressed_but_not_an_attendee_is_not_auto_imported(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Row 113: being a To/Cc recipient is not the same as being
        invited. Only ATTENDEE resolves an identity for an automatic
        import -- the to/cc fallback stays available to
        resolve_attendee_identity()'s callers, just never for this one,
        since it is also the delivery vector for an unbounded RRULE (row
        108): anyone who merely addresses the mailbox could otherwise put
        an event straight into the calendar."""
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
                data=_request_ics(uid, attendee="someone-else@example.com"),
                message_id_hdr="<m2b@example.com>", to_addrs=["freddy@work.example"],
            )

        await _handler(migrated_db).handle_message_event(_insert_event(mail_id, account_id))

        async with migrated_db.session() as session:
            row = (
                await session.execute(
                    text("SELECT status, object_id FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).one()
            object_count = (
                await session.execute(
                    text("SELECT count(*) FROM dav_objects WHERE collection_id = :c"),
                    {"c": collection_id},
                )
            ).scalar_one()
        assert row.status == "unlinked"
        assert row.object_id is None
        assert object_count == 0

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
        event resolves by UID to the object already there -- never a
        second copy -- even though a REQUEST against an existing object
        is never applied automatically and needs confirming either way.
        The first identity's own invitation already imported is
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
        assert status == "pending_review"

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
    async def test_cancel_of_a_known_event_needs_confirming(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Row 107 v2: even a CANCEL naming the real organizer is never
        applied automatically -- the same `.ics` that hands a forger a
        UID hands them the ORGANIZER address too, so matching one
        authenticates nothing. It becomes a row a person confirms
        through POST .../import instead; the object is untouched until
        then."""
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
            original_data = _stored_event_data(uid)
            existing_object_id = await _seed_existing_object(
                session, dav_account_id=dav_account_id, collection_id=collection_id,
                uid=uid, data=original_data,
            )
            cancel_mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_cancel_ics(uid), message_id_hdr="<m6b@example.com>",
            )

        await _handler(migrated_db).handle_message_event(_insert_event(cancel_mail_id, account_id))

        async with migrated_db.session() as session:
            status, object_id = (
                await session.execute(
                    text("SELECT status, object_id FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).one()
            deleted_at, stored = (
                await session.execute(
                    text("SELECT deleted_at, data FROM dav_objects WHERE id = :id"),
                    {"id": existing_object_id},
                )
            ).one()
        assert status == "pending_review"
        assert object_id == existing_object_id
        assert deleted_at is None
        assert stored == original_data  # untouched -- nothing applies without confirming
        master, _ = ical.parse_master_and_exceptions(stored)
        assert master.status == "confirmed"

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
                message_id_hdr="<m8b@example.com>", from_addr="freddy@work.example",
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


class TestAuthentication:
    """Row 107: an incoming REQUEST/CANCEL against a UID this application
    already holds is never applied automatically, whoever it claims to
    be from -- ORGANIZER equality was tried and does not authenticate
    anything a co-attendee could not already produce, since the same
    `.ics` that hands them the UID hands them the ORGANIZER address too.
    Every such message becomes 'pending_review'; the object is left
    untouched either way. REPLY is the one method still gated (on the
    message's own sender) and still applies automatically."""

    @pytest.mark.asyncio
    async def test_request_from_a_different_organizer_is_not_applied(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """A naive forgery -- a co-attendee (or anyone else) who merely
        knows the UID invents their own ORGANIZER -- cannot move or
        rewrite the event, however high its SEQUENCE."""
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
            original_data = _stored_event_data(uid)
            existing_object_id = await _seed_existing_object(
                session, dav_account_id=dav_account_id, collection_id=collection_id,
                uid=uid, data=original_data,
            )
            attack_mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_attacker_request_ics(uid), message_id_hdr="<attack1@evil.example>",
            )

        await _handler(migrated_db).handle_message_event(_insert_event(attack_mail_id, account_id))

        async with migrated_db.session() as session:
            status = (
                await session.execute(
                    text("SELECT status FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).scalar_one()
            stored = (
                await session.execute(
                    text("SELECT data FROM dav_objects WHERE id = :id"),
                    {"id": existing_object_id},
                )
            ).scalar_one()
        assert status == "pending_review"
        assert stored == original_data
        master, _ = ical.parse_master_and_exceptions(stored)
        assert master.organizer is not None and master.organizer.email == "anna@example.com"
        assert master.summary == "Kickoff"

    @pytest.mark.asyncio
    async def test_request_from_a_co_attendee_copying_the_real_organizer_is_not_applied(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The attack that actually mattered: a co-attendee of the real
        meeting already holds the UID *and* the ORGANIZER address, both
        lines in the `.ics` they themselves received. Matching ORGANIZER
        against the stored object's own is not a barrier to this at all
        -- the fix has to be that a REQUEST against an existing object
        never applies automatically, full stop, not that it applies
        only when the forger bothers to copy the real ORGANIZER line."""
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
            original_data = _stored_event_data(uid)
            existing_object_id = await _seed_existing_object(
                session, dav_account_id=dav_account_id, collection_id=collection_id,
                uid=uid, data=original_data,
            )
            # Same UID, same ORGANIZER as the stored object -- exactly
            # what a co-attendee's own copy of the invitation carries --
            # with a forged SUMMARY/DTSTART and a high SEQUENCE.
            forged_but_correctly_organized = (
                "BEGIN:VCALENDAR\r\n"
                "VERSION:2.0\r\n"
                "PRODID:-//Test//EN\r\n"
                "METHOD:REQUEST\r\n"
                "BEGIN:VEVENT\r\n"
                f"UID:{uid}\r\n"
                "DTSTAMP:20260901T120000Z\r\n"
                "DTSTART:20260911T030000Z\r\n"
                "DTEND:20260911T040000Z\r\n"
                "SUMMARY:MOVED - see attacker.example\r\n"
                "SEQUENCE:99\r\n"
                "ORGANIZER;CN=Anna Mueller:mailto:anna@example.com\r\n"
                "END:VEVENT\r\n"
                "END:VCALENDAR\r\n"
            )
            attack_mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=forged_but_correctly_organized,
                message_id_hdr="<attack-coattendee@example.com>",
            )

        await _handler(migrated_db).handle_message_event(_insert_event(attack_mail_id, account_id))

        async with migrated_db.session() as session:
            status = (
                await session.execute(
                    text("SELECT status FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).scalar_one()
            stored = (
                await session.execute(
                    text("SELECT data FROM dav_objects WHERE id = :id"),
                    {"id": existing_object_id},
                )
            ).scalar_one()
        assert status == "pending_review"
        assert stored == original_data
        master, _ = ical.parse_master_and_exceptions(stored)
        assert master.summary == "Kickoff"

    @pytest.mark.asyncio
    async def test_cancel_from_a_different_organizer_is_not_applied(
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
            original_data = _stored_event_data(uid)
            existing_object_id = await _seed_existing_object(
                session, dav_account_id=dav_account_id, collection_id=collection_id,
                uid=uid, data=original_data,
            )
            attack_mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_attacker_cancel_ics(uid), message_id_hdr="<attack2@evil.example>",
            )

        await _handler(migrated_db).handle_message_event(_insert_event(attack_mail_id, account_id))

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
        assert status == "pending_review"
        assert deleted_at is None
        assert stored == original_data
        master, _ = ical.parse_master_and_exceptions(stored)
        assert master.status == "confirmed"

    @pytest.mark.asyncio
    async def test_reply_from_a_different_attendee_is_not_applied(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """`_apply()` would otherwise write attendees[0]'s PARTSTAT
        unconditionally -- a third party who knows the UID could mark any
        attendee DECLINED by claiming to be them in the REPLY body while
        actually mailing from somewhere else."""
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
            original_data = _stored_event_data(uid)
            existing_object_id = await _seed_existing_object(
                session, dav_account_id=dav_account_id, collection_id=collection_id,
                uid=uid, data=original_data,
            )
            # The REPLY claims to speak for freddy@work.example, but the
            # message itself (from_addr, set by _seed_message_with_ics)
            # arrives from anna@example.com.
            attack_mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_reply_ics(uid, attendee="freddy@work.example", partstat="DECLINED"),
                message_id_hdr="<attack3@example.com>",
            )

        await _handler(migrated_db).handle_message_event(_insert_event(attack_mail_id, account_id))

        async with migrated_db.session() as session:
            status = (
                await session.execute(
                    text("SELECT status FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).scalar_one()
            stored = (
                await session.execute(
                    text("SELECT data FROM dav_objects WHERE id = :id"),
                    {"id": existing_object_id},
                )
            ).scalar_one()
        assert status == "unauthorized"
        assert stored == original_data

    @pytest.mark.asyncio
    async def test_uid_lookup_is_not_reachable_across_unrelated_accounts(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The scope half of row 107: a UID collision with an object under
        a DAV account only linked to a different mail account's identity
        must not resolve at all -- it has to be treated as a fresh
        invitation (or ignored, for CANCEL/REPLY), never as "the existing
        object", however matching ORGANIZER would otherwise pass."""
        uid = _new_uid()
        async with migrated_db.session() as session:
            # The object lives under a DAV account linked to a *different*
            # mail account's identity.
            _other_account_id, _other_folder_id, other_identity_id = (
                await _seed_mail_account_folder_and_identity(session, email="other@work.example")
            )
            assert other_identity_id is not None
            dav_account_id, collection_id = await _seed_dav_calendar(session)
            await _link_intake_calendar(
                session, collection_id=collection_id, identity_id=other_identity_id,
            )
            original_data = _stored_event_data(uid)
            existing_object_id = await _seed_existing_object(
                session, dav_account_id=dav_account_id, collection_id=collection_id,
                uid=uid, data=original_data,
            )

            # The receiving mail account has no calendar linked to this
            # DAV account at all -- an invitation naming the same UID
            # arrives here regardless (a stranger can put any UID in the
            # .ics they send).
            account_id, folder_id, identity_id = await _seed_mail_account_folder_and_identity(
                session, email="freddy@work.example",
            )
            assert identity_id is not None
            mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                # Same UID, same ORGANIZER as the stored object -- if scope
                # were not enforced, this alone would pass the organizer
                # check and update the wrong account's object.
                data=_request_ics(uid), message_id_hdr="<cross-account@example.com>",
            )

        await _handler(migrated_db).handle_message_event(_insert_event(mail_id, account_id))

        async with migrated_db.session() as session:
            status = (
                await session.execute(
                    text("SELECT status FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).scalar_one()
            stored = (
                await session.execute(
                    text("SELECT data FROM dav_objects WHERE id = :id"),
                    {"id": existing_object_id},
                )
            ).scalar_one()
        # Not "updated" -- the sending account has no reachable link to
        # that object, so this is unlinked (no intake calendar of its
        # own), and the other account's object is untouched.
        assert status == "unlinked"
        assert stored == original_data


class TestPendingRetry:
    """Row 114: calendar_intake's gate row is written before _apply()
    writes anything, but never with a terminal status _apply() has not
    actually reached -- a decision that writes something is inserted as
    'pending' and only promoted once that write lands."""

    @pytest.mark.asyncio
    async def test_a_stuck_pending_row_is_retried_and_promoted(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Simulates the crash the finding describes: the gate row exists
        at 'pending', the object write it describes never happened. A
        later call for the same message must apply it and promote the
        row, not treat the gate as already closed."""
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
            message_id_hdr = "<pending1@example.com>"
            mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_request_ics(uid), message_id_hdr=message_id_hdr,
            )
            # compute_msg_key() returns the header verbatim whenever one
            # is present, so the literal passed to _seed_message_with_ics
            # above already is the key -- no need to round-trip through a
            # second session to read the row back before it is committed.
            msg_key = compute_msg_key(
                account_id=account_id, message_id_hdr=message_id_hdr,
                from_addr=None, subject=None, received_at=None, size_bytes=None,
            )
            assert msg_key == message_id_hdr
            await session.execute(
                text(
                    "INSERT INTO calendar_intake "
                    "(account_id, msg_key, ical_uid, method, sequence, status) "
                    "VALUES (:account_id, :msg_key, :uid, 'REQUEST', 0, 'pending')"
                ),
                {"account_id": account_id, "msg_key": msg_key, "uid": uid},
            )
            await session.commit()

        await _handler(migrated_db).process_arrival(mail_id, account_id)

        async with migrated_db.session() as session:
            row = (
                await session.execute(
                    text("SELECT status, object_id FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).one()
            object_count = (
                await session.execute(
                    text("SELECT count(*) FROM dav_objects WHERE collection_id = :c"),
                    {"c": collection_id},
                )
            ).scalar_one()
        assert row.status == "imported"
        assert row.object_id is not None
        assert object_count == 1

    @pytest.mark.asyncio
    async def test_a_terminal_row_is_never_reapplied(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The never-classify-twice gate still holds for anything that
        actually finished -- only 'pending' is retryable."""
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
                data=_request_ics(uid), message_id_hdr="<pending2@example.com>",
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
        assert object_count == 1


class TestReadOnlyIntakeCalendar:
    @pytest.mark.asyncio
    async def test_read_only_intake_calendar_is_not_auto_imported(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Row 114: manual import already refuses a read-only calendar
        with 400 -- automatic import silently skipped this check, so the
        write dead-lettered on the server with nothing telling anyone."""
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
            await session.execute(
                text("UPDATE dav_collections SET read_only = true WHERE id = :id"),
                {"id": collection_id},
            )
            mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_request_ics(uid), message_id_hdr="<readonly1@example.com>",
            )

        await _handler(migrated_db).handle_message_event(_insert_event(mail_id, account_id))

        async with migrated_db.session() as session:
            row = (
                await session.execute(
                    text("SELECT status, object_id FROM calendar_intake WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).one()
            object_count = (
                await session.execute(
                    text("SELECT count(*) FROM dav_objects WHERE collection_id = :c"),
                    {"c": collection_id},
                )
            ).scalar_one()
        assert row.status == "unlinked"
        assert row.object_id is None
        assert object_count == 0


class TestFindCalendarAttachment:
    """Row 114: a Google-shaped invitation carries both a bare
    text/calendar part and an application/ics one -- picking between them
    has to be deterministic, not whatever an unordered `.first()` returns."""

    @pytest.mark.asyncio
    async def test_text_calendar_wins_over_application_ics_regardless_of_insert_order(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        uid_text = _new_uid()
        uid_ics = _new_uid()
        async with migrated_db.session() as session:
            account_id, folder_id, _identity_id = await _seed_mail_account_folder_and_identity(
                session, email=None,
            )
            mail_id = await _seed_message_with_ics(
                session, account_id=account_id, folder_id=folder_id,
                data=_request_ics(uid_ics), message_id_hdr="<multi-part@example.com>",
            )
            # _seed_message_with_ics already inserted the application/ics
            # part (content_type='text/calendar' in its own helper --
            # add the other content type here) with a lower id (inserted
            # first); text/calendar must still win despite that.
            await session.execute(
                text(
                    "UPDATE attachments SET content_type = 'application/ics' "
                    "WHERE message_id = :message_id"
                ),
                {"message_id": mail_id},
            )
            await session.execute(
                text(
                    "INSERT INTO attachments (id, message_id, filename, content_type, data) "
                    "VALUES (:id, :message_id, NULL, 'text/calendar', :data)"
                ),
                {
                    "id": uuid.uuid4(), "message_id": mail_id,
                    "data": _request_ics(uid_text).encode("utf-8"),
                },
            )

        found = await _handler(migrated_db).find_calendar_attachment(mail_id)
        assert found is not None
        assert ical.get_uid(found) == uid_text


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
