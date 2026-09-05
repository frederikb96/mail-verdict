"""
Cross-account isolation and accurate bulk-action counting on the message
API, against a real database.

Every direct database call (seeding and post-assertions alike) goes
through `client.portal.call(...)` -- the same event loop TestClient's own
portal uses to run the app -- rather than being awaited directly in the
test function. Doing it directly would give the shared `migrated_db`
engine's asyncpg connections two different event loops to run on and
fail with "attached to a different loop" the moment a second one touches
it (see test_pipeline_api.py's `client` fixture docstring for the same
note).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from mail_verdict.api.mails import account_router, router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Message
from tests.pg.test_bulk_actions_and_outbox import _seed_account_two_folders, _seed_messages


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """A single persistent TestClient portal for the whole test."""
    app = FastAPI()
    app.include_router(router)
    app.include_router(account_router)
    with TestClient(app) as c:
        yield c


async def _seed_two_accounts_and_one_message(
    migrated_db: DatabaseConnection,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """account_a with one message in its inbox, and account_b's junk
    folder -- the cross-account move target."""
    async with migrated_db.session() as session:
        account_a, inbox_a, _junk_a = await _seed_account_two_folders(session)
        _account_b, _inbox_b, junk_b = await _seed_account_two_folders(session)
        (message_id,) = await _seed_messages(session, account_a, inbox_a, 1)
        await session.commit()
    return account_a, inbox_a, junk_b, message_id


async def _seed_two_accounts_and_two_messages(
    migrated_db: DatabaseConnection,
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID], uuid.UUID]:
    """account_a with two messages in its inbox, and account_b's junk
    folder -- the cross-account bulk move target."""
    async with migrated_db.session() as session:
        account_a, inbox_a, _junk_a = await _seed_account_two_folders(session)
        _account_b, _inbox_b, junk_b = await _seed_account_two_folders(session)
        ids = await _seed_messages(session, account_a, inbox_a, 2)
        await session.commit()
    return account_a, inbox_a, ids, junk_b


async def _seed_own_and_foreign_messages(
    migrated_db: DatabaseConnection,
) -> tuple[uuid.UUID, list[uuid.UUID], uuid.UUID]:
    """account_a with two messages, plus one message belonging to a
    separate account_b -- the id a bulk request should never touch."""
    async with migrated_db.session() as session:
        account_a, inbox_a, _junk_a = await _seed_account_two_folders(session)
        account_b, inbox_b, _junk_b = await _seed_account_two_folders(session)
        own_ids = await _seed_messages(session, account_a, inbox_a, 2)
        (foreign_id,) = await _seed_messages(session, account_b, inbox_b, 1)
        await session.commit()
    return account_a, own_ids, foreign_id


async def _seed_one_account_and_two_messages(
    migrated_db: DatabaseConnection,
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    async with migrated_db.session() as session:
        account_a, inbox_a, _junk_a = await _seed_account_two_folders(session)
        ids = await _seed_messages(session, account_a, inbox_a, 2)
        await session.commit()
    return account_a, ids


async def _folder_id_of(migrated_db: DatabaseConnection, message_id: uuid.UUID) -> uuid.UUID:
    async with migrated_db.session() as session:
        result = await session.execute(select(Message.folder_id).where(Message.id == message_id))
        return result.scalar_one()


async def _folder_ids_of(
    migrated_db: DatabaseConnection, message_ids: list[uuid.UUID],
) -> set[uuid.UUID]:
    async with migrated_db.session() as session:
        result = await session.execute(
            select(Message.folder_id).where(Message.id.in_(message_ids))
        )
        return set(result.scalars().all())


async def _is_seen_by_id(
    migrated_db: DatabaseConnection, message_ids: list[uuid.UUID],
) -> dict[uuid.UUID, bool]:
    async with migrated_db.session() as session:
        result = await session.execute(
            select(Message.id, Message.is_seen).where(Message.id.in_(message_ids))
        )
        return dict(result.all())


async def _is_expunged_by_id(
    migrated_db: DatabaseConnection, message_ids: list[uuid.UUID],
) -> dict[uuid.UUID, bool]:
    async with migrated_db.session() as session:
        result = await session.execute(
            select(Message.id, Message.expunged_at).where(Message.id.in_(message_ids))
        )
        return {mid: expunged_at is not None for mid, expunged_at in result.all()}


async def _seed_one_account_two_messages_in_one_folder(
    migrated_db: DatabaseConnection,
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    async with migrated_db.session() as session:
        account_a, inbox_a, _junk_a = await _seed_account_two_folders(session)
        ids = await _seed_messages(session, account_a, inbox_a, 2)
        await session.commit()
    return account_a, inbox_a, ids


class TestSingleMoveCrossAccount:
    def test_move_into_another_accounts_folder_is_rejected(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        _account_a, inbox_a, junk_b, message_id = client.portal.call(
            _seed_two_accounts_and_one_message, migrated_db,
        )

        with patch("mail_verdict.api.mails.get_db_connection", return_value=migrated_db):
            resp = client.post(
                f"/messages/{message_id}/action",
                json={"action": "move", "target_folder_id": str(junk_b)},
            )
        assert resp.status_code == 400
        assert client.portal.call(_folder_id_of, migrated_db, message_id) == inbox_a


class TestBulkMoveCrossAccount:
    def test_move_into_another_accounts_folder_is_rejected(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_a, inbox_a, ids, junk_b = client.portal.call(
            _seed_two_accounts_and_two_messages, migrated_db,
        )

        with patch("mail_verdict.api.mails.get_db_connection", return_value=migrated_db):
            resp = client.post(
                f"/accounts/{account_a}/messages/bulk-action",
                json={
                    "action": "move", "ids": [str(i) for i in ids],
                    "target_folder_id": str(junk_b),
                },
            )
        assert resp.status_code == 400
        assert client.portal.call(_folder_ids_of, migrated_db, ids) == {inbox_a}


class TestBulkActionExplicitIdsAreAccountScoped:
    def test_ids_belonging_to_another_account_are_excluded(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """A client naming an id from another account must not touch it,
        and affected_count must reflect only what actually happened."""
        account_a, own_ids, foreign_id = client.portal.call(
            _seed_own_and_foreign_messages, migrated_db,
        )

        with patch("mail_verdict.api.mails.get_db_connection", return_value=migrated_db):
            resp = client.post(
                f"/accounts/{account_a}/messages/bulk-action",
                json={
                    "action": "mark_read",
                    "ids": [str(i) for i in (*own_ids, foreign_id)],
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["affected_count"] == len(own_ids)  # foreign_id never counted

        by_id = client.portal.call(_is_seen_by_id, migrated_db, [*own_ids, foreign_id])
        assert all(by_id[i] for i in own_ids)
        assert by_id[foreign_id] is False  # untouched


class TestBulkActionAffectedCountReflectsReality:
    def test_a_nonexistent_id_is_not_counted_as_affected(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_a, ids = client.portal.call(_seed_one_account_and_two_messages, migrated_db)

        made_up_id = uuid.uuid4()
        with patch("mail_verdict.api.mails.get_db_connection", return_value=migrated_db):
            resp = client.post(
                f"/accounts/{account_a}/messages/bulk-action",
                json={
                    "action": "mark_read",
                    "ids": [str(i) for i in (*ids, made_up_id)],
                },
            )
        assert resp.status_code == 200
        # 2 supplied real ids + 1 nonexistent one; only 2 can ever be
        # affected, and the response must say so rather than 3.
        assert resp.json()["affected_count"] == len(ids)


class TestBulkActionConfirmMessageCount:
    """confirm_message_count -- the guard against an irreversible
    whole-scope write (emptying a folder, most concretely) acting on a
    different count than whatever number a caller showed a user before
    sending the request."""

    def test_a_mismatched_count_is_rejected_and_nothing_is_touched(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_a, inbox_a, ids = client.portal.call(
            _seed_one_account_two_messages_in_one_folder, migrated_db,
        )
        snapshot_at = datetime.now(UTC).isoformat()

        with patch("mail_verdict.api.mails.get_db_connection", return_value=migrated_db):
            resp = client.post(
                f"/accounts/{account_a}/messages/bulk-action",
                json={
                    "action": "expunge",
                    "scope": {
                        "folder_id": str(inbox_a), "filter": "all", "snapshot_at": snapshot_at,
                    },
                    # Both messages actually resolve; this names a count
                    # that disagrees with it, as if a message had left the
                    # folder between whatever showed this count and now.
                    "confirm_message_count": 5,
                },
            )
        assert resp.status_code == 409
        assert "2" in resp.text  # the count the response should report back

        by_id = client.portal.call(_is_expunged_by_id, migrated_db, ids)
        assert not any(by_id.values()), "a rejected request must not have expunged anything"

    def test_a_matching_count_proceeds(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_a, inbox_a, ids = client.portal.call(
            _seed_one_account_two_messages_in_one_folder, migrated_db,
        )
        snapshot_at = datetime.now(UTC).isoformat()

        with patch("mail_verdict.api.mails.get_db_connection", return_value=migrated_db):
            resp = client.post(
                f"/accounts/{account_a}/messages/bulk-action",
                json={
                    "action": "expunge",
                    "scope": {
                        "folder_id": str(inbox_a), "filter": "all", "snapshot_at": snapshot_at,
                    },
                    "confirm_message_count": len(ids),
                },
            )
        assert resp.status_code == 200
        assert resp.json()["affected_count"] == len(ids)

        by_id = client.portal.call(_is_expunged_by_id, migrated_db, ids)
        assert all(by_id.values())

    def test_omitted_entirely_runs_no_check(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """Every other bulk action sends nothing here and must keep working
        exactly as before -- the field is opt-in, not a new requirement on
        the whole endpoint."""
        account_a, ids = client.portal.call(_seed_one_account_and_two_messages, migrated_db)

        with patch("mail_verdict.api.mails.get_db_connection", return_value=migrated_db):
            resp = client.post(
                f"/accounts/{account_a}/messages/bulk-action",
                json={"action": "mark_read", "ids": [str(i) for i in ids]},
            )
        assert resp.status_code == 200
        assert resp.json()["affected_count"] == len(ids)
