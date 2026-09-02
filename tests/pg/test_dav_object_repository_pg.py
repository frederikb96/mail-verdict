"""
DavObjectRepository.list_in_collections() against a real database -- row
109: the window filter that keeps a month view from parsing every object
in every visible calendar.

No live DAV server backs these tests (the same choice
test_calendar_intake_pg.py and test_calendar_events_api_pg.py make):
dtstart/dtend/is_recurring are PostIMAP's own parse of `data`, so a row
this suite creates through create_object() never gets them filled in.
Every test here seeds dav_objects directly with those columns set, the
same pattern those two files use for the parsed columns they need.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.calendar.repository import DavObjectRepository
from mail_verdict.database.connection import DatabaseConnection


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


async def _seed_object(
    session: AsyncSession, *, dav_account_id: uuid.UUID, collection_id: uuid.UUID,
    dtstart: datetime | None, dtend: datetime | None, is_recurring: bool = False,
) -> uuid.UUID:
    object_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dav_objects "
            "(id, account_id, collection_id, kind, data, dtstart, dtend, is_recurring) "
            "VALUES (:id, :account_id, :collection_id, 'calendar', "
            " 'BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n', :dtstart, :dtend, :is_recurring)"
        ),
        {
            "id": object_id, "account_id": dav_account_id, "collection_id": collection_id,
            "dtstart": dtstart, "dtend": dtend, "is_recurring": is_recurring,
        },
    )
    return object_id


class TestWindowFilter:
    @pytest.mark.asyncio
    async def test_object_outside_the_window_is_excluded(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            dav_account_id, collection_id = await _seed_dav_calendar(session)
            inside_id = await _seed_object(
                session, dav_account_id=dav_account_id, collection_id=collection_id,
                dtstart=datetime(2026, 9, 10, 9, tzinfo=timezone.utc),
                dtend=datetime(2026, 9, 10, 10, tzinfo=timezone.utc),
            )
            await _seed_object(
                session, dav_account_id=dav_account_id, collection_id=collection_id,
                dtstart=datetime(2024, 1, 1, 9, tzinfo=timezone.utc),
                dtend=datetime(2024, 1, 1, 10, tzinfo=timezone.utc),
            )

        objects = await DavObjectRepository(migrated_db).list_in_collections(
            [collection_id],
            datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        assert {o.id for o in objects} == {inside_id}

    @pytest.mark.asyncio
    async def test_recurring_master_is_kept_regardless_of_its_own_dtstart(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """A recurring master's own dtstart is its series' first
        occurrence, which can be years before a window that still
        contains a later occurrence -- it must never be filtered on its
        own dtstart the way a non-recurring object is."""
        async with migrated_db.session() as session:
            dav_account_id, collection_id = await _seed_dav_calendar(session)
            recurring_id = await _seed_object(
                session, dav_account_id=dav_account_id, collection_id=collection_id,
                dtstart=datetime(2020, 1, 1, 9, tzinfo=timezone.utc),
                dtend=datetime(2020, 1, 1, 10, tzinfo=timezone.utc), is_recurring=True,
            )

        objects = await DavObjectRepository(migrated_db).list_in_collections(
            [collection_id],
            datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        assert {o.id for o in objects} == {recurring_id}

    @pytest.mark.asyncio
    async def test_pending_object_with_no_parsed_dtstart_yet_is_kept(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """dtstart/dtend are NULL until PostIMAP's outbound processor
        claims a just-inserted row -- excluding NULL rows would make a
        freshly created event vanish from the very view that should show
        it, for as long as that parse takes."""
        async with migrated_db.session() as session:
            dav_account_id, collection_id = await _seed_dav_calendar(session)
            pending_id = await _seed_object(
                session, dav_account_id=dav_account_id, collection_id=collection_id,
                dtstart=None, dtend=None,
            )

        objects = await DavObjectRepository(migrated_db).list_in_collections(
            [collection_id],
            datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        assert {o.id for o in objects} == {pending_id}

    @pytest.mark.asyncio
    async def test_dtstart_only_event_with_null_dtend_is_kept(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Row 109 re-verification: PostIMAP only ever writes dtend from
        an explicit DTEND property -- a DURATION-only event, a
        DTSTART-only one, and the canonical single-day all-day
        `DTSTART;VALUE=DATE` with neither all leave dtend NULL. Without
        COALESCE, `dtend > window_start` is NULL under three-valued
        logic and the row is silently excluded, even though dtstart sits
        inside the window and the event is present on the server."""
        async with migrated_db.session() as session:
            dav_account_id, collection_id = await _seed_dav_calendar(session)
            all_day_id = await _seed_object(
                session, dav_account_id=dav_account_id, collection_id=collection_id,
                dtstart=datetime(2026, 9, 10, tzinfo=timezone.utc), dtend=None,
            )

        objects = await DavObjectRepository(migrated_db).list_in_collections(
            [collection_id],
            datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        assert {o.id for o in objects} == {all_day_id}

    @pytest.mark.asyncio
    async def test_dtstart_only_event_before_the_window_is_still_excluded(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """COALESCE must not turn the NULL-dtend fix into a fix that never
        excludes anything -- an old dtstart-only event still has to fall
        outside a window that starts after it ends."""
        async with migrated_db.session() as session:
            dav_account_id, collection_id = await _seed_dav_calendar(session)
            await _seed_object(
                session, dav_account_id=dav_account_id, collection_id=collection_id,
                dtstart=datetime(2024, 1, 1, tzinfo=timezone.utc), dtend=None,
            )

        objects = await DavObjectRepository(migrated_db).list_in_collections(
            [collection_id],
            datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        assert objects == []

    @pytest.mark.asyncio
    async def test_no_window_returns_everything(self, migrated_db: DatabaseConnection) -> None:
        async with migrated_db.session() as session:
            dav_account_id, collection_id = await _seed_dav_calendar(session)
            old_id = await _seed_object(
                session, dav_account_id=dav_account_id, collection_id=collection_id,
                dtstart=datetime(2024, 1, 1, tzinfo=timezone.utc),
                dtend=datetime(2024, 1, 1, 1, tzinfo=timezone.utc),
            )

        objects = await DavObjectRepository(migrated_db).list_in_collections([collection_id])
        assert {o.id for o in objects} == {old_id}
