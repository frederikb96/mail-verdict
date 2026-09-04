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
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, desc, func, or_, select
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
    Identity,
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
        """A UID already held somewhere in one DAV account -- see the
        contract's "(account_id, uid)" index. Scoped to a single DAV
        account; find_by_uid_anywhere() is what intake actually needs,
        since a hand-imported event may live under a different DAV
        account than the one an identity's intake calendar points at."""
        async with self._db.session() as session:
            result = await session.execute(
                select(DavObject).where(
                    DavObject.account_id == account_id,
                    DavObject.uid == uid,
                    DavObject.deleted_at.is_(None),
                )
            )
            return result.scalar_one_or_none()

    async def find_by_uid_anywhere(self, uid: str) -> DavObject | None:
        """A UID already held in any dav_objects row of any DAV account --
        for a person choosing to import or retry a specific message
        (api/invitations.py's POST .../import). A hand-imported event can
        live under any DAV account regardless of how (or whether) it is
        linked to a mail identity, and the design settles on "update in
        place where it lives, whatever the mapping says" for that case.
        This is safe here because a human, not an emailed .ics, is the
        one deciding to write -- find_by_uid_reachable() below is what
        the *automatic* listener path needs instead, since there nothing
        but the UID itself is under this application's control."""
        async with self._db.session() as session:
            result = await session.execute(
                select(DavObject)
                .where(DavObject.uid == uid, DavObject.deleted_at.is_(None))
                .order_by(DavObject.created_at)
            )
            return result.scalars().first()

    async def find_by_uid_reachable(self, uid: str, account_id: uuid.UUID) -> DavObject | None:
        """The UID lookup calendar/intake.py's automatic listener needs: a
        UID already held in a dav_objects row of a DAV account reachable
        from this mail account -- the hand-imported case the design
        settles on ("update in place where it lives, whatever the mapping
        says"), and what keeps two of a user's own addresses being
        invited to the same event from producing two copies.

        Scoped to DAV accounts that have at least one collection linked
        (calendar_prefs.identity_id) to one of this mail account's own
        identities -- an emailed invitation naming a UID that happens to
        collide with an object in a DAV account belonging to a different
        mail account's identities must never resolve to that object.
        Without this, any DAV account in the whole database was in scope,
        so an invitation to mail account A could rewrite an object under
        an unrelated mail account's calendar. find_by_uid_anywhere() above
        is what a person explicitly choosing to import one message still
        uses -- that scoping only matters where nothing but the UID is
        under this application's control."""
        async with self._db.session() as session:
            reachable_dav_accounts = (
                select(DavCollection.account_id)
                .join(CalendarPrefs, CalendarPrefs.collection_id == DavCollection.id)
                .join(Identity, Identity.id == CalendarPrefs.identity_id)
                .where(Identity.account_id == account_id)
            )
            result = await session.execute(
                select(DavObject)
                .where(
                    DavObject.uid == uid,
                    DavObject.deleted_at.is_(None),
                    DavObject.account_id.in_(reachable_dav_accounts),
                )
                .order_by(DavObject.created_at)
            )
            return result.scalars().first()

    async def list_in_collections(
        self, collection_ids: list[uuid.UUID],
        window_start: datetime | None = None, window_end: datetime | None = None,
    ) -> list[DavObject]:
        """Live objects across a set of visible calendars -- the raw
        material calendar/ical.py's expand_instances() then windows.

        Filtered in SQL to what a window could actually contain when one
        is given: a recurring master (any occurrence could fall inside
        the window, so it always qualifies) or a non-recurring object
        whose own [dtstart, dtend) overlaps it. dtstart/dtend/is_recurring
        are PostIMAP's own read-only parse of `data`
        (idx_dav_objects_collection_dtstart indexes exactly this), so this
        never touches recurrence rules itself. Without a window, every
        live object is returned -- a NULL dtstart/is_recurring (an insert
        or move still pending its outbound sync) is also kept in that
        case, since parsed columns lag the write by milliseconds and a
        just-created event should still render.

        dtend is COALESCEd to dtstart because PostIMAP only ever writes it
        from an explicit DTEND property (codec.ts reads
        getFirstProperty("dtend") and nothing else) -- a DURATION-only
        event, a DTSTART-only one, or the canonical single-day all-day
        `DTSTART;VALUE=DATE` with neither, all leave dtend NULL. Comparing
        a NULL dtend directly (`dtend > window_start`) is NULL under SQL's
        three-valued logic, so the row is silently excluded -- the
        `dtstart IS NULL` branch above does not rescue it either, since
        dtstart is set. Every one of those shapes vanished from the
        calendar entirely, still present on the server and in this very
        table, before COALESCE.
        """
        if not collection_ids:
            return []
        async with self._db.session() as session:
            stmt = select(DavObject).where(
                DavObject.collection_id.in_(collection_ids),
                DavObject.deleted_at.is_(None),
            )
            if window_start is not None and window_end is not None:
                effective_dtend = func.coalesce(DavObject.dtend, DavObject.dtstart)
                stmt = stmt.where(
                    or_(
                        DavObject.is_recurring.is_(True),
                        DavObject.dtstart.is_(None),
                        and_(
                            DavObject.dtstart < window_end, effective_dtend >= window_start,
                        ),
                    )
                )
            result = await session.execute(stmt)
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

    async def find_by_email(self, email: str) -> DavObject | None:
        """The first contact carrying this address, case-insensitively --
        what a sender's avatar/name lookup resolves against. Several
        contacts sharing one address is possible; the caller only needs
        one match, so this isn't the paged `search_email_hits` above.

        Prefiltered in SQL the same way `search_email_hits` does (ILIKE on
        the emails array flattened to text), then matched exactly in
        Python -- an address book is small enough that pulling the few
        ILIKE hits into Python for an exact, case-insensitive compare is
        simpler and less fragile than composing a per-element `lower()`
        comparison against a Postgres array in SQLAlchemy."""
        needle = email.strip().lower()
        if not needle:
            return None
        async with self._db.session() as session:
            result = await session.execute(
                select(DavObject)
                .where(
                    DavObject.kind == "addressbook",
                    DavObject.deleted_at.is_(None),
                    func.array_to_string(DavObject.emails, ",").ilike(f"%{needle}%"),
                )
                .order_by(DavObject.summary)
            )
            for obj in result.scalars().all():
                if any((addr or "").strip().lower() == needle for addr in obj.emails or []):
                    return obj
        return None

    async def get_unresolved_errors(
        self, object_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, str]:
        """
        object_id -> error, for every object with an unresolved failed
        write -- a dav_notifications row with reverted_at IS NULL, the
        same "may still be sitting in the column looking applied" state
        the contract's Pending writes and conflicts section describes.
        Batched over the whole visible set rather than one query per
        object. api/invitations.py is the one caller: whether a manual
        import dead-lettered, which is genuinely still wrong until
        retried -- not the reverted case get_write_errors() below also
        covers, which is a conflict PostIMAP already resolved.
        """
        if not object_ids:
            return {}
        async with self._db.session() as session:
            result = await session.execute(
                select(DavNotification.object_id, DavNotification.error)
                .where(
                    DavNotification.object_id.in_(object_ids),
                    DavNotification.reverted_at.is_(None),
                )
                .order_by(DavNotification.created_at)
            )
            # Last write wins if an object has more than one unresolved row.
            return {row.object_id: row.error for row in result.all() if row.object_id is not None}

    async def get_write_errors(
        self, object_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, str]:
        """
        object_id -> a message worth surfacing at the event level, for
        every object with a dav_notifications row -- both an unresolved
        failure (reverted_at IS NULL, the write may still be sitting in
        the column) and a 412 conflict PostIMAP already resolved by
        re-reading the server's copy over it (reverted_at IS NOT NULL).

        Per the contract's "Pending writes and conflicts", a 412 means
        the server won and the row's own edit is gone -- silently, unless
        something says so. get_unresolved_errors() above excludes exactly
        that case, which is the one case a user's edit was actually
        thrown away rather than merely still pending. Batched over the
        whole visible set rather than one query per object.
        """
        if not object_ids:
            return {}
        async with self._db.session() as session:
            result = await session.execute(
                select(
                    DavNotification.object_id, DavNotification.error,
                    DavNotification.reverted_at,
                )
                .where(DavNotification.object_id.in_(object_ids))
                .order_by(DavNotification.created_at)
            )
            # Last write wins if an object has more than one notification.
            messages: dict[uuid.UUID, str] = {}
            for row in result.all():
                if row.object_id is None:
                    continue
                messages[row.object_id] = (
                    "Your last change to this event was replaced by the server's own version."
                    if row.reverted_at is not None else row.error
                )
            return messages


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
