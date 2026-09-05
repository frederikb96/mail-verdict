"""
GET /api/accounts/:id/messages?around=... -- a page centred on a message
rather than the newest edge, against a real Postgres schema. Reuses
test_threaded_messages.py's own seed helpers, since the trap this exists
for (threaded mode resolving to a thread's own representative row, never
the named message) needs the same thread/count shape those tests already
build.

Every test that drives the endpoint through TestClient seeds via
`client.portal.call(...)` -- the same event loop the client's own portal
uses -- rather than awaiting migrated_db directly in the test function.
Doing it directly gives the shared engine's asyncpg connections two
different event loops to run on and fails with "attached to a different
loop" (see test_mails_api_pg.py's own module docstring for the same note).
"""

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

from mail_verdict.api.mails import (
    _list_messages_around,
    _resolve_around_flat,
    _resolve_around_threaded,
    account_router,
)
from mail_verdict.database.connection import DatabaseConnection
from tests.pg.test_threaded_messages import _seed_account_and_inbox, _seed_message

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)

_MAILS_TARGET = "mail_verdict.api.mails.get_db_connection"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(account_router)
    with TestClient(app) as c:
        yield c


async def _seed_flat_run(
    session: AsyncSession, account_id: uuid.UUID, folder_id: uuid.UUID, count: int,
) -> list[uuid.UUID]:
    """`count` ordinary messages, oldest to newest, one minute apart --
    index 0 is oldest, index count-1 is newest. Each its own thread, since
    flat mode never groups by it."""
    ids = []
    for i in range(count):
        message_id = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, thread_id=uuid.uuid4(),
            received_at=_BASE_TIME + timedelta(minutes=i), is_seen=True, uid=i + 1,
        )
        ids.append(message_id)
    return ids


class TestResolveAroundFlat:
    @pytest.mark.asyncio
    async def test_resolves_to_the_message_itself_when_it_matches(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id = await _seed_account_and_inbox(session)
            ids = await _seed_flat_run(session, account_id, inbox_id, 5)
            await session.commit()

        async with migrated_db.session() as session:
            resolved = await _resolve_around_flat(
                session, account_id, inbox_id, None, None, ids[2],
            )
        assert resolved is not None
        assert resolved.id == ids[2]

    @pytest.mark.asyncio
    async def test_none_when_filtered_out_by_folder(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id = await _seed_account_and_inbox(session)
            ids = await _seed_flat_run(session, account_id, inbox_id, 1)
            other_folder_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO folders (id, account_id, imap_name, special_use) "
                    "VALUES (:id, :account_id, 'Sent', 'sent')"
                ),
                {"id": other_folder_id, "account_id": account_id},
            )
            await session.commit()

        async with migrated_db.session() as session:
            resolved = await _resolve_around_flat(
                session, account_id, other_folder_id, None, None, ids[0],
            )
        assert resolved is None

    @pytest.mark.asyncio
    async def test_none_when_the_message_does_not_exist(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id = await _seed_account_and_inbox(session)
            await session.commit()

        async with migrated_db.session() as session:
            resolved = await _resolve_around_flat(
                session, account_id, inbox_id, None, None, uuid.uuid4(),
            )
        assert resolved is None


class TestResolveAroundThreaded:
    @pytest.mark.asyncio
    async def test_resolves_to_the_threads_own_latest_message_not_the_target(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The trap: a threaded list shows one row per thread, so a target
        buried in the middle of its own thread is represented by that
        thread's newest message -- a different row entirely."""
        async with migrated_db.session() as session:
            account_id, inbox_id = await _seed_account_and_inbox(session)
            thread_id = uuid.uuid4()
            oldest = await _seed_message(
                session, account_id=account_id, folder_id=inbox_id, thread_id=thread_id,
                received_at=_BASE_TIME, is_seen=True, uid=1,
            )
            middle = await _seed_message(
                session, account_id=account_id, folder_id=inbox_id, thread_id=thread_id,
                received_at=_BASE_TIME + timedelta(minutes=1), is_seen=True, uid=2,
            )
            newest = await _seed_message(
                session, account_id=account_id, folder_id=inbox_id, thread_id=thread_id,
                received_at=_BASE_TIME + timedelta(minutes=2), is_seen=False, uid=3,
            )
            await session.commit()

        async with migrated_db.session() as session:
            resolved = await _resolve_around_threaded(
                session, account_id, inbox_id, None, None, middle,
            )
        assert resolved is not None
        representative, thread_count, unread_in_thread = resolved
        assert representative.id == newest
        assert representative.id != middle
        assert representative.id != oldest
        assert thread_count == 3
        assert unread_in_thread == 1

    @pytest.mark.asyncio
    async def test_none_when_the_thread_has_no_member_matching_the_filters(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """A thread existing is not enough -- every one of its messages
        can still be filtered out (here, an unread-only view over an
        all-read thread), and that must read as absent, not empty."""
        async with migrated_db.session() as session:
            account_id, inbox_id = await _seed_account_and_inbox(session)
            thread_id = uuid.uuid4()
            target = await _seed_message(
                session, account_id=account_id, folder_id=inbox_id, thread_id=thread_id,
                received_at=_BASE_TIME, is_seen=True, uid=1,
            )
            await session.commit()

        async with migrated_db.session() as session:
            # An unread-only view over a thread that is entirely read.
            resolved = await _resolve_around_threaded(
                session, account_id, inbox_id, False, None, target,
            )
        assert resolved is None


class TestListMessagesAround:
    @pytest.mark.asyncio
    async def test_the_window_splits_roughly_evenly_around_the_target(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id = await _seed_account_and_inbox(session)
            ids = await _seed_flat_run(session, account_id, inbox_id, 9)
            await session.commit()

        async with migrated_db.session() as session:
            response = await _list_messages_around(
                session, account_id, inbox_id, False, None, None, ids[4], limit=5,
            )

        # Newest-first, target in the middle: two newer, target, two older.
        assert [m.id for m in response.messages] == [
            ids[6], ids[5], ids[4], ids[3], ids[2],
        ]
        assert response.has_more is True
        assert response.has_more_newer is True
        assert response.next_cursor == str(ids[2])
        assert response.prev_cursor == str(ids[6])

    @pytest.mark.asyncio
    async def test_a_target_near_the_newest_edge_has_nothing_newer(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id = await _seed_account_and_inbox(session)
            ids = await _seed_flat_run(session, account_id, inbox_id, 3)
            await session.commit()

        async with migrated_db.session() as session:
            response = await _list_messages_around(
                session, account_id, inbox_id, False, None, None, ids[2], limit=5,
            )

        assert [m.id for m in response.messages] == [ids[2], ids[1], ids[0]]
        assert response.has_more is False
        assert response.has_more_newer is False
        assert response.prev_cursor is None

    async def _seed_around_not_a_member(
        self, migrated_db: DatabaseConnection,
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        async with migrated_db.session() as session:
            account_id, inbox_id = await _seed_account_and_inbox(session)
            other_folder_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO folders (id, account_id, imap_name, special_use) "
                    "VALUES (:id, :account_id, 'Sent', 'sent')"
                ),
                {"id": other_folder_id, "account_id": account_id},
            )
            (target_id,) = await _seed_flat_run(session, account_id, inbox_id, 1)
            await session.commit()
        return account_id, other_folder_id, target_id

    def test_not_a_member_of_this_list_is_a_404(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        # Seeding goes through the app's own portal, not a directly awaited
        # migrated_db call -- the two run on different event loops, and the
        # shared engine's asyncpg connections belong to whichever loop
        # opened them (see this file's own module docstring).
        account_id, other_folder_id, target_id = client.portal.call(
            self._seed_around_not_a_member, migrated_db,
        )

        with patch(_MAILS_TARGET, return_value=migrated_db):
            resp = client.get(
                f"/accounts/{account_id}/messages",
                params={"folder_id": str(other_folder_id), "around": str(target_id)},
            )
        assert resp.status_code == 404, resp.text
        assert "not a member" in resp.json()["detail"]

    async def _seed_around_and_before(
        self, migrated_db: DatabaseConnection,
    ) -> tuple[uuid.UUID, list[uuid.UUID]]:
        async with migrated_db.session() as session:
            account_id, inbox_id = await _seed_account_and_inbox(session)
            ids = await _seed_flat_run(session, account_id, inbox_id, 2)
            await session.commit()
        return account_id, ids

    def test_around_together_with_before_is_a_400(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, ids = client.portal.call(self._seed_around_and_before, migrated_db)

        with patch(_MAILS_TARGET, return_value=migrated_db):
            resp = client.get(
                f"/accounts/{account_id}/messages",
                params={"around": str(ids[0]), "before": str(ids[1])},
            )
        assert resp.status_code == 400, resp.text

    async def _seed_around_threaded_trap(
        self, migrated_db: DatabaseConnection,
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        async with migrated_db.session() as session:
            account_id, inbox_id = await _seed_account_and_inbox(session)
            thread_id = uuid.uuid4()
            await _seed_message(
                session, account_id=account_id, folder_id=inbox_id, thread_id=thread_id,
                received_at=_BASE_TIME, is_seen=True, uid=1,
            )
            middle = await _seed_message(
                session, account_id=account_id, folder_id=inbox_id, thread_id=thread_id,
                received_at=_BASE_TIME + timedelta(minutes=1), is_seen=True, uid=2,
            )
            newest = await _seed_message(
                session, account_id=account_id, folder_id=inbox_id, thread_id=thread_id,
                received_at=_BASE_TIME + timedelta(minutes=2), is_seen=False, uid=3,
            )
            await session.commit()
        return account_id, middle, newest

    def test_end_to_end_through_the_real_endpoint_threaded(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """The same trap proven at the function level in
        TestResolveAroundThreaded, now through the actual HTTP endpoint a
        browser calls."""
        account_id, middle, newest = client.portal.call(
            self._seed_around_threaded_trap, migrated_db,
        )

        with patch(_MAILS_TARGET, return_value=migrated_db):
            resp = client.get(
                f"/accounts/{account_id}/messages",
                params={"threaded": "true", "around": str(middle)},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [m["id"] for m in body["messages"]] == [str(newest)]
        assert body["messages"][0]["thread_count"] == 3
        assert body["messages"][0]["unread_in_thread"] == 1
