"""
Reads and owned-table writes for calendars and contacts.

Everything reading dav_accounts/dav_collections/dav_objects/dav_notifications
lives here rather than in database/repository.py -- a domain-scoped split,
the same reasoning spam/ and settings/ already follow. Writes onto those
PostIMAP-owned tables still go exclusively through postimap/actions.py;
what's here is SELECT-only for them, and full read/write for the three
tables this application owns (calendar_prefs, calendar_intake,
calendar_replies).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import (
    CalendarIntake,
    CalendarLinksRevision,
    CalendarPrefs,
    CalendarReply,
    DavAccount,
    DavCollection,
    DavNotification,
    DavObject,
)

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection


class DavAccountRepository:
    """Reads on dav_accounts/dav_collections/dav_notifications. Writes go
    through postimap/actions.py."""

    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    async def list_all(self) -> list[DavAccount]:
        async with self._db.session() as session:
            result = await session.execute(select(DavAccount).order_by(DavAccount.name))
            return list(result.scalars().all())

    async def get_by_id(self, dav_account_id: uuid.UUID) -> DavAccount | None:
        async with self._db.session() as session:
            result = await session.execute(
                select(DavAccount).where(DavAccount.id == dav_account_id)
            )
            return result.scalar_one_or_none()

    async def list_collections(
        self, dav_account_id: uuid.UUID, *, kind: str | None = None,
    ) -> list[DavCollection]:
        async with self._db.session() as session:
            stmt = (
                select(DavCollection)
                .where(
                    DavCollection.account_id == dav_account_id,
                    DavCollection.deleted_at.is_(None),
                )
                .order_by(DavCollection.display_name, DavCollection.slug)
            )
            if kind is not None:
                stmt = stmt.where(DavCollection.kind == kind)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def list_notifications(
        self, dav_account_id: uuid.UUID, *, unacknowledged_only: bool = False, limit: int = 100,
    ) -> list[DavNotification]:
        async with self._db.session() as session:
            stmt = (
                select(DavNotification)
                .where(DavNotification.account_id == dav_account_id)
                .order_by(desc(DavNotification.created_at), desc(DavNotification.id))
                .limit(limit)
            )
            if unacknowledged_only:
                stmt = stmt.where(DavNotification.acknowledged_at.is_(None))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def unacknowledged_notification_count(self, dav_account_id: uuid.UUID) -> int:
        async with self._db.session() as session:
            result = await session.execute(
                select(func.count(DavNotification.id)).where(
                    DavNotification.account_id == dav_account_id,
                    DavNotification.acknowledged_at.is_(None),
                )
            )
            return result.scalar_one()


class CollectionRepository:
    """Reads on dav_collections, scoped for the calendars/addressbooks API."""

    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    async def get_by_id(self, collection_id: uuid.UUID) -> DavCollection | None:
        async with self._db.session() as session:
            result = await session.execute(
                select(DavCollection).where(DavCollection.id == collection_id)
            )
            return result.scalar_one_or_none()

    async def list_by_kind(self, kind: str) -> list[tuple[DavCollection, DavAccount]]:
        """Every non-deleted collection of one kind, joined to its DAV
        account for the *_name fields the API responses carry."""
        async with self._db.session() as session:
            result = await session.execute(
                select(DavCollection, DavAccount)
                .join(DavAccount, DavCollection.account_id == DavAccount.id)
                .where(DavCollection.kind == kind, DavCollection.deleted_at.is_(None))
                .order_by(DavAccount.name, DavCollection.display_name)
            )
            return [(c, a) for c, a in result.all()]


class DavObjectRepository:
    """Reads on dav_objects. Writes go through postimap/actions.py."""

    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    async def get_by_id(self, object_id: uuid.UUID) -> DavObject | None:
        async with self._db.session() as session:
            result = await session.execute(select(DavObject).where(DavObject.id == object_id))
            return result.scalar_one_or_none()

    async def find_by_account_and_uid(
        self, account_id: uuid.UUID, uid: str,
    ) -> DavObject | None:
        """The UID lookup intake needs: a UID already held anywhere in the
        account, whatever calendar it lives in -- see the contract's
        "(account_id, uid)" index."""
        async with self._db.session() as session:
            result = await session.execute(
                select(DavObject).where(
                    DavObject.account_id == account_id,
                    DavObject.uid == uid,
                    DavObject.deleted_at.is_(None),
                )
            )
            return result.scalar_one_or_none()

    async def list_in_collections(
        self, collection_ids: list[uuid.UUID],
    ) -> list[DavObject]:
        """Every live object across a set of visible calendars -- the raw
        material calendar/ical.py's expand_instances() then windows."""
        if not collection_ids:
            return []
        async with self._db.session() as session:
            result = await session.execute(
                select(DavObject).where(
                    DavObject.collection_id.in_(collection_ids),
                    DavObject.deleted_at.is_(None),
                )
            )
            return list(result.scalars().all())

    async def search_contacts(
        self, addressbook_ids: list[uuid.UUID] | None, query: str | None, *,
        limit: int, offset: int,
    ) -> tuple[list[DavObject], bool]:
        """Contacts, ILIKE on summary/emails when a query is given.
        Returns (page, has_more)."""
        async with self._db.session() as session:
            stmt = select(DavObject).where(
                DavObject.kind == "addressbook", DavObject.deleted_at.is_(None),
            )
            if addressbook_ids is not None:
                stmt = stmt.where(DavObject.collection_id.in_(addressbook_ids))
            if query:
                pattern = f"%{query}%"
                stmt = stmt.where(
                    or_(
                        DavObject.summary.ilike(pattern),
                        func.array_to_string(DavObject.emails, ",").ilike(pattern),
                    )
                )
            stmt = stmt.order_by(DavObject.summary).limit(limit + 1).offset(offset)
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
            has_more = len(rows) > limit
            return rows[:limit], has_more

    async def search_email_hits(
        self, query: str, *, limit: int,
    ) -> list[DavObject]:
        """Contacts whose summary or any email matches -- the raw rows the
        compose autocomplete flattens to one row per address."""
        pattern = f"%{query}%"
        async with self._db.session() as session:
            result = await session.execute(
                select(DavObject)
                .where(
                    DavObject.kind == "addressbook",
                    DavObject.deleted_at.is_(None),
                    or_(
                        DavObject.summary.ilike(pattern),
                        func.array_to_string(DavObject.emails, ",").ilike(pattern),
                    ),
                )
                .order_by(DavObject.summary)
                .limit(limit)
            )
            return list(result.scalars().all())


class CalendarPrefsRepository:
    """calendar_prefs -- MailVerdict-owned, full read/write."""

    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    async def get(self, collection_id: uuid.UUID) -> CalendarPrefs | None:
        async with self._db.session() as session:
            result = await session.execute(
                select(CalendarPrefs).where(CalendarPrefs.collection_id == collection_id)
            )
            return result.scalar_one_or_none()

    async def list_all(self) -> dict[uuid.UUID, CalendarPrefs]:
        async with self._db.session() as session:
            result = await session.execute(select(CalendarPrefs))
            return {p.collection_id: p for p in result.scalars().all()}

    async def get_intake_collection_id(self, identity_id: uuid.UUID) -> uuid.UUID | None:
        async with self._db.session() as session:
            collection_id: uuid.UUID | None = await session.scalar(
                select(CalendarPrefs.collection_id).where(
                    CalendarPrefs.identity_id == identity_id, CalendarPrefs.intake.is_(True),
                )
            )
            return collection_id

    async def update(self, collection_id: uuid.UUID, **fields: Any) -> CalendarPrefs:
        """
        Upsert calendar_prefs, clearing any other calendar's intake claim
        on the same identity first when this update sets intake=true --
        the partial unique index only allows one true row at a time, the
        same discipline Identity.is_default uses.
        """
        async with self._db.session() as session:
            if fields.get("intake") is True:
                identity_id = fields.get("identity_id")
                if identity_id is None:
                    existing = await session.execute(
                        select(CalendarPrefs.identity_id).where(
                            CalendarPrefs.collection_id == collection_id,
                        )
                    )
                    identity_id = existing.scalar_one_or_none()
                if identity_id is not None:
                    await session.execute(
                        pg_insert(CalendarPrefs)
                        .values(collection_id=collection_id)
                        .on_conflict_do_nothing(index_elements=["collection_id"])
                    )
                    result = await session.execute(
                        select(CalendarPrefs).where(
                            CalendarPrefs.identity_id == identity_id,
                            CalendarPrefs.intake.is_(True),
                            CalendarPrefs.collection_id != collection_id,
                        )
                    )
                    for other in result.scalars().all():
                        other.intake = False
                    await session.flush()

            stmt = (
                pg_insert(CalendarPrefs)
                .values(collection_id=collection_id, **fields)
                .on_conflict_do_update(index_elements=["collection_id"], set_=fields)
                .returning(CalendarPrefs)
            )
            result = await session.execute(stmt)
            return result.scalar_one()

    async def unlink_identity(self, identity_id: uuid.UUID) -> None:
        """Clear every calendar's link to an identity being deleted --
        the FK's ON DELETE SET NULL already does this at the database
        level; this is for a caller that wants it done inside the same
        transaction as some other change."""
        async with self._db.session() as session:
            result = await session.execute(
                select(CalendarPrefs).where(CalendarPrefs.identity_id == identity_id)
            )
            for row in result.scalars().all():
                row.identity_id = None
                row.intake = False


class CalendarIntakeRepository:
    """calendar_intake -- MailVerdict-owned, the never-classify-twice gate
    for invitation emails."""

    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    async def get_by_account_msg_key(
        self, account_id: uuid.UUID, msg_key: str,
    ) -> CalendarIntake | None:
        async with self._db.session() as session:
            result = await session.execute(
                select(CalendarIntake).where(
                    CalendarIntake.account_id == account_id, CalendarIntake.msg_key == msg_key,
                )
            )
            return result.scalar_one_or_none()

    async def create_if_absent(self, **fields: Any) -> tuple[CalendarIntake, bool]:
        """
        Insert a row, or return the existing one -- the gate itself.
        `created` is False whenever a redelivered or resynced message
        already has a row, which is what tells the caller to stop rather
        than process the message a second time.
        """
        async with self._db.session() as session:
            stmt = (
                pg_insert(CalendarIntake)
                .values(**fields)
                .on_conflict_do_nothing(constraint="uq_calendar_intake_account_msg_key")
                .returning(CalendarIntake)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is not None:
                return row, True

            existing = await session.execute(
                select(CalendarIntake).where(
                    CalendarIntake.account_id == fields["account_id"],
                    CalendarIntake.msg_key == fields["msg_key"],
                )
            )
            return existing.scalar_one(), False

    async def update_status(
        self, intake_id: uuid.UUID, *, status: str, reason: str | None = None, **fields: Any,
    ) -> None:
        async with self._db.session() as session:
            result = await session.execute(
                select(CalendarIntake).where(CalendarIntake.id == intake_id)
            )
            row = result.scalar_one()
            row.status = status
            row.reason = reason
            for key, value in fields.items():
                setattr(row, key, value)

    async def get_by_object(self, object_id: uuid.UUID) -> CalendarIntake | None:
        """The row an event's invitation card renders from, when it has one."""
        async with self._db.session() as session:
            result = await session.execute(
                select(CalendarIntake)
                .where(CalendarIntake.object_id == object_id)
                .order_by(desc(CalendarIntake.created_at))
                .limit(1)
            )
            return result.scalar_one_or_none()


class CalendarReplyRepository:
    """calendar_replies -- MailVerdict-owned, insert-only."""

    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    async def create(
        self, *, object_id: uuid.UUID, recurrence_id: str | None,
        identity_id: uuid.UUID, partstat: str, outbox_id: uuid.UUID,
    ) -> CalendarReply:
        async with self._db.session() as session:
            reply = CalendarReply(
                object_id=object_id, recurrence_id=recurrence_id,
                identity_id=identity_id, partstat=partstat, outbox_id=outbox_id,
            )
            session.add(reply)
            await session.flush()
            await session.refresh(reply)
            return reply

    async def get_latest(
        self, object_id: uuid.UUID, recurrence_id: str | None,
    ) -> CalendarReply | None:
        """The last reply attempt for this instance, any identity -- an
        event has at most one attendee among this application's
        identities in the common case, so "any identity" and "the
        identity that RSVP'd" coincide."""
        async with self._db.session() as session:
            result = await session.execute(
                select(CalendarReply)
                .where(
                    CalendarReply.object_id == object_id,
                    CalendarReply.recurrence_id == recurrence_id,
                )
                .order_by(desc(CalendarReply.created_at))
                .limit(1)
            )
            return result.scalar_one_or_none()


class CalendarLinksRevisionRepository:
    """The single-row optimistic-concurrency counter PUT /calendar/links checks."""

    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    async def current(self) -> int:
        async with self._db.session() as session:
            revision: int | None = await session.scalar(select(CalendarLinksRevision.revision))
            return revision or 0

    async def bump(self, session: AsyncSession) -> int:
        """Increment the counter as part of an already-open transaction --
        callers pass their own session so this commits atomically with the
        calendar_prefs rows it is meant to guard."""
        row = await session.get(CalendarLinksRevision, True)
        if row is None:
            row = CalendarLinksRevision(revision=1)
            session.add(row)
        else:
            row.revision += 1
        await session.flush()
        new_revision: int = row.revision
        return new_revision
