"""
A large batch of calendar writes must reach storage in order,
with nothing lost and nothing duplicated -- against a real database,
not a mock.

MailVerdict's own contribution to that guarantee is the write path from
an API/MCP call into Postgres: each call opens its own session, and
DatabaseConnection.session() commits on success or rolls back and raises
on failure, so a caller either sees its write land or sees the error --
never a silent no-op. What happens to a row after it lands (PostIMAP's
own outbound queue draining it to the real CalDAV server, strictly in
order) is the separate repository's own contract guarantee
(consumer-contract.md: "Several writes to one object before the queue
drains are applied in the order they were made") and is not what these
tests exercise.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
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
from mail_verdict.postimap.actions import create_object

_TARGET = "mail_verdict.api.calendar_events.get_db_connection"

_BATCH_SIZE = 100
_CONCURRENT_SIZE = 25


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


async def _seed(db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID]:
    async with db.session() as session:
        dav_account_id, collection_id = await _seed_calendar(session)
        await session.commit()
    return dav_account_id, collection_id


async def _live_object_count(db: DatabaseConnection, collection_id: uuid.UUID) -> int:
    async with db.session() as session:
        result = await session.execute(
            text(
                "SELECT count(*) FROM dav_objects "
                "WHERE collection_id = :id AND deleted_at IS NULL"
            ),
            {"id": collection_id},
        )
        return int(result.scalar_one())


class TestSequentialBatch:
    """The realistic shape: an MCP client awaits each tool call's result
    before issuing the next, so a hundred creates followed by a hundred
    edits happen one at a time -- exactly what the API/TestClient path
    below drives."""

    def test_a_hundred_sequential_creates_all_land_with_no_loss_or_duplication(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        _dav_account_id, collection_id = client.portal.call(_seed, migrated_db)

        object_ids: list[str] = []
        with patch(_TARGET, return_value=migrated_db):
            for i in range(_BATCH_SIZE):
                resp = client.post(
                    "/calendar/events",
                    json={
                        "calendar_id": str(collection_id), "summary": f"Event {i}",
                        "dtstart": "2026-09-10T10:00:00+00:00",
                        "dtend": "2026-09-10T11:00:00+00:00",
                    },
                )
                assert resp.status_code == 201, resp.text
                object_ids.append(resp.json()["object_id"])

        # No lost writes (every call got back a 201) and no duplicated
        # rows (the count in storage matches exactly, and every UID this
        # batch produced is distinct).
        assert len(object_ids) == len(set(object_ids)) == _BATCH_SIZE
        count = client.portal.call(_live_object_count, migrated_db, collection_id)
        assert count == _BATCH_SIZE

    def test_a_hundred_sequential_edits_each_apply_exactly_once(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        _dav_account_id, collection_id = client.portal.call(_seed, migrated_db)
        object_ids: list[str] = []
        with patch(_TARGET, return_value=migrated_db):
            for i in range(_BATCH_SIZE):
                resp = client.post(
                    "/calendar/events",
                    json={
                        "calendar_id": str(collection_id), "summary": f"Event {i}",
                        "dtstart": "2026-09-10T10:00:00+00:00",
                        "dtend": "2026-09-10T11:00:00+00:00",
                    },
                )
                assert resp.status_code == 201, resp.text
                object_ids.append(resp.json()["object_id"])

            for object_id, i in zip(object_ids, range(_BATCH_SIZE), strict=True):
                resp = client.patch(
                    f"/calendar/events/{object_id}",
                    json={"summary": f"Event {i} (edited)", "scope": "all"},
                )
                assert resp.status_code == 200, resp.text
                body = resp.json()
                # Not lost (the edit landed) and not double-applied (one
                # edit bumps SEQUENCE by exactly one).
                assert body["summary"] == f"Event {i} (edited)"
                assert body["sequence"] == 1

        count = client.portal.call(_live_object_count, migrated_db, collection_id)
        assert count == _BATCH_SIZE


class TestConcurrentBatch:
    """MailVerdict does not itself serialize calendar writes -- nothing
    stops an agent from firing several tool calls at once. This drives
    the same postimap/actions.py write functions create_event() calls
    underneath, concurrently, against the real connection pool
    migrated_db hands out, to prove a burst does not lose or duplicate a
    row even when several sessions are open at the same instant."""

    @pytest.mark.asyncio
    async def test_concurrent_creates_all_land_with_distinct_uids(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        dav_account_id, collection_id = await _seed(migrated_db)

        async def _create_one(i: int) -> tuple[uuid.UUID, str]:
            data = ical.build_new_event(
                summary=f"Concurrent {i}",
                dtstart=datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc),
                dtend=datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc),
            )
            async with migrated_db.session() as session:
                obj = await create_object(
                    session, dav_account_id=dav_account_id,
                    collection_id=collection_id, data=data,
                )
                return obj.id, ical.get_uid(data)

        results = await asyncio.gather(*(_create_one(i) for i in range(_CONCURRENT_SIZE)))

        object_ids = [r[0] for r in results]
        uids = [r[1] for r in results]
        assert len(object_ids) == len(set(object_ids)) == _CONCURRENT_SIZE
        assert len(uids) == len(set(uids)) == _CONCURRENT_SIZE

        count = await _live_object_count(migrated_db, collection_id)
        assert count == _CONCURRENT_SIZE
