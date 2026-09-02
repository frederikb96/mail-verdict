"""
calendar_prefs/calendar_intake/calendar_replies -- the schema-level
guarantees the migration is responsible for, independent of any API.

Runs against migrated_db (the owner connection): these tables are
MailVerdict-owned, so they carry no PostIMAP grant boundary to prove --
that is what tests/pg/test_grant_boundary.py is for, and it only applies
to postimap/actions.py's writes onto PostIMAP's own tables.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import CalendarIntake, CalendarPrefs, CalendarReply, Identity


async def _seed_account(session, imap_user: str = "user@example.com") -> uuid.UUID:  # type: ignore[no-untyped-def]
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


async def _seed_identity(session, account_id: uuid.UUID, email: str) -> uuid.UUID:  # type: ignore[no-untyped-def]
    identity_id = uuid.uuid4()
    session.add(Identity(id=identity_id, account_id=account_id, email=email))
    await session.flush()
    return identity_id


@pytest.mark.asyncio
async def test_at_most_one_intake_calendar_per_identity(
    migrated_db: DatabaseConnection,
) -> None:
    """uq_calendar_prefs_intake -- the partial unique index -- refuses a
    second intake=true row for the same identity."""
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        identity_id = await _seed_identity(session, account_id, "freddy@work.example")
        session.add(CalendarPrefs(collection_id=uuid.uuid4(), identity_id=identity_id, intake=True))
        await session.flush()
        await session.commit()

    with pytest.raises(IntegrityError):
        async with migrated_db.session() as session:
            session.add(
                CalendarPrefs(collection_id=uuid.uuid4(), identity_id=identity_id, intake=True)
            )
            await session.flush()


@pytest.mark.asyncio
async def test_two_identities_may_each_have_their_own_intake_calendar(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        first = await _seed_identity(session, account_id, "first@work.example")
        second = await _seed_identity(session, account_id, "second@work.example")
        session.add(CalendarPrefs(collection_id=uuid.uuid4(), identity_id=first, intake=True))
        session.add(CalendarPrefs(collection_id=uuid.uuid4(), identity_id=second, intake=True))
        await session.flush()
        await session.commit()


@pytest.mark.asyncio
async def test_deleting_an_identity_unlinks_its_calendars(
    migrated_db: DatabaseConnection,
) -> None:
    """identity_id's ON DELETE SET NULL -- a calendar survives its identity
    being deleted, just no longer linked to (or receiving invitations
    for) it."""
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        identity_id = await _seed_identity(session, account_id, "gone@work.example")
        collection_id = uuid.uuid4()
        session.add(
            CalendarPrefs(collection_id=collection_id, identity_id=identity_id, intake=True)
        )
        await session.flush()
        await session.commit()

    async with migrated_db.session() as session:
        await session.execute(
            text("DELETE FROM identities WHERE id = :id"), {"id": identity_id},
        )
        await session.commit()

    async with migrated_db.session() as session:
        result = await session.execute(
            select(CalendarPrefs.identity_id, CalendarPrefs.intake)
            .where(CalendarPrefs.collection_id == collection_id)
        )
        row = result.one()
        assert row.identity_id is None
        # intake stays true -- SET NULL only clears the identity link, not
        # the calendar's own claim to being an intake calendar. The API
        # layer treats "intake with no identity" as effectively none.
        assert row.intake is True


@pytest.mark.asyncio
async def test_calendar_intake_msg_key_is_unique_per_account(
    migrated_db: DatabaseConnection,
) -> None:
    """uq_calendar_intake_account_msg_key -- the never-classify-twice gate.
    A redelivered or resynced invitation for a message already processed
    is refused at the database level, not merely by application logic."""
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        await session.execute(
            insert(CalendarIntake).values(
                account_id=account_id, msg_key="<invite@example.com>", ical_uid="uid-1",
                method="REQUEST", status="imported",
            )
        )
        await session.commit()

    with pytest.raises(IntegrityError):
        async with migrated_db.session() as session:
            await session.execute(
                insert(CalendarIntake).values(
                    account_id=account_id, msg_key="<invite@example.com>", ical_uid="uid-1",
                    method="REQUEST", status="imported",
                )
            )
            await session.commit()


@pytest.mark.asyncio
async def test_calendar_intake_rejects_an_unknown_method(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        with pytest.raises(IntegrityError):
            await session.execute(
                insert(CalendarIntake).values(
                    account_id=account_id, msg_key="<bad@example.com>", ical_uid="uid-2",
                    method="PUBLISH", status="imported",
                )
            )
            await session.flush()


@pytest.mark.asyncio
async def test_calendar_replies_records_every_attempt(
    migrated_db: DatabaseConnection,
) -> None:
    """Insert-only -- calling respond() twice after a failed send leaves
    two rows, not one overwritten row, so a retry history survives."""
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        identity_id = await _seed_identity(session, account_id, "freddy@work.example")
        object_id = uuid.uuid4()
        session.add(CalendarReply(
            object_id=object_id, identity_id=identity_id,
            partstat="accepted", outbox_id=uuid.uuid4(),
        ))
        session.add(CalendarReply(
            object_id=object_id, identity_id=identity_id,
            partstat="accepted", outbox_id=uuid.uuid4(),
        ))
        await session.flush()
        await session.commit()

    async with migrated_db.session() as session:
        result = await session.execute(
            select(CalendarReply).where(CalendarReply.object_id == object_id)
        )
        assert len(list(result.scalars().all())) == 2


@pytest.mark.asyncio
async def test_deleting_an_identity_unlinks_its_replies(
    migrated_db: DatabaseConnection,
) -> None:
    """identity_id's ON DELETE SET NULL -- the reply survives
    its identity being deleted, the same as calendar_prefs above,
    instead of the RSVP history the table exists to keep being deleted
    along with the identity that sent it."""
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        identity_id = await _seed_identity(session, account_id, "gone2@work.example")
        object_id = uuid.uuid4()
        session.add(CalendarReply(
            object_id=object_id, identity_id=identity_id,
            partstat="accepted", outbox_id=uuid.uuid4(),
        ))
        await session.flush()
        await session.commit()

    async with migrated_db.session() as session:
        await session.execute(
            text("DELETE FROM identities WHERE id = :id"), {"id": identity_id},
        )
        await session.commit()

    async with migrated_db.session() as session:
        result = await session.execute(
            select(CalendarReply.identity_id, CalendarReply.partstat)
            .where(CalendarReply.object_id == object_id)
        )
        row = result.one()
        assert row.identity_id is None
        assert row.partstat == "accepted"
