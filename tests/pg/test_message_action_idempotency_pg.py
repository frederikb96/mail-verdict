"""
A keyed message action is applied once, however many times its request
arrives: a repeat carrying the same idempotency_key is answered with the
first response and writes nothing, through the same endpoints the web and
iPhone clients call.

Each test proves "nothing written" by changing the row back underneath the
repeat -- a move repeated for real would move it again, a replay leaves it
where the test put it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.mails import account_router, router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Message, MessageActionSubmission
from mail_verdict.mail_actions.submissions import prune_submissions_once

_MAILS_TARGET = "mail_verdict.api.mails.get_db_connection"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)
    app.include_router(account_router)
    with TestClient(app) as c:
        yield c


async def _seed(session: AsyncSession) -> dict[str, uuid.UUID]:
    """An inactive account -- PostIMAP leaves its rows exactly as written --
    with two plain folders and one message in the first."""
    ids = {
        "account": uuid.uuid4(), "source": uuid.uuid4(), "target": uuid.uuid4(),
        "message": uuid.uuid4(),
    }
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password, "
            "is_active) VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'), false)"
        ),
        {"id": ids["account"], "name": f"acct-{ids['account']}"},
    )
    for key, name in (("source", "Projects"), ("target", "Receipts")):
        await session.execute(
            text(
                "INSERT INTO folders (id, account_id, imap_name) "
                "VALUES (:id, :account_id, :name)"
            ),
            {"id": ids[key], "account_id": ids["account"], "name": name},
        )
    await session.execute(
        text(
            "INSERT INTO messages (id, account_id, folder_id, imap_uid, thread_id, message_id, "
            "subject, received_at) VALUES (:id, :account_id, :folder_id, 1, :thread_id, "
            ":msg_id, 'Once', now())"
        ),
        {
            "id": ids["message"], "account_id": ids["account"], "folder_id": ids["source"],
            "thread_id": uuid.uuid4(), "msg_id": f"<{ids['message']}@example.com>",
        },
    )
    return ids


async def _setup(migrated_db: DatabaseConnection) -> dict[str, uuid.UUID]:
    async with migrated_db.session() as session:
        return await _seed(session)


async def _folder_of(migrated_db: DatabaseConnection, message_id: uuid.UUID) -> uuid.UUID:
    async with migrated_db.session() as session:
        folder_id = await session.scalar(select(Message.folder_id).where(Message.id == message_id))
    assert folder_id is not None
    return folder_id


async def _put_back(
    migrated_db: DatabaseConnection, message_id: uuid.UUID, folder_id: uuid.UUID,
) -> None:
    async with migrated_db.session() as session:
        await session.execute(
            text("UPDATE messages SET folder_id = :folder_id WHERE id = :id"),
            {"folder_id": folder_id, "id": message_id},
        )


async def _submission_count(migrated_db: DatabaseConnection, key: str) -> int:
    async with migrated_db.session() as session:
        count = await session.scalar(
            select(func.count()).select_from(MessageActionSubmission).where(
                MessageActionSubmission.idempotency_key == uuid.UUID(key),
            )
        )
    return int(count or 0)


class TestSingleMessageAction:
    def test_the_same_key_twice_moves_once_and_replays_the_response(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        ids = client.portal.call(_setup, migrated_db)
        body = {
            "action": "move", "target_folder_id": str(ids["target"]),
            "idempotency_key": str(uuid.uuid4()),
        }
        with patch(_MAILS_TARGET, return_value=migrated_db):
            first = client.post(f"/messages/{ids['message']}/action", json=body)
            assert first.status_code == 200, first.text
            assert client.portal.call(_folder_of, migrated_db, ids["message"]) == ids["target"]
            client.portal.call(_put_back, migrated_db, ids["message"], ids["source"])
            second = client.post(f"/messages/{ids['message']}/action", json=body)

        assert second.status_code == 200, second.text
        assert second.json() == first.json()
        assert client.portal.call(_folder_of, migrated_db, ids["message"]) == ids["source"]

    def test_no_key_acts_every_time(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """The field is optional: a client that sends none keeps today's behaviour."""
        ids = client.portal.call(_setup, migrated_db)
        body = {"action": "move", "target_folder_id": str(ids["target"])}
        with patch(_MAILS_TARGET, return_value=migrated_db):
            assert client.post(f"/messages/{ids['message']}/action", json=body).status_code == 200
            client.portal.call(_put_back, migrated_db, ids["message"], ids["source"])
            assert client.post(f"/messages/{ids['message']}/action", json=body).status_code == 200

        assert client.portal.call(_folder_of, migrated_db, ids["message"]) == ids["target"]

    def test_a_key_reused_for_a_different_request_is_refused(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        ids = client.portal.call(_setup, migrated_db)
        key = str(uuid.uuid4())
        with patch(_MAILS_TARGET, return_value=migrated_db):
            flag = client.post(
                f"/messages/{ids['message']}/action",
                json={"action": "flag", "idempotency_key": key},
            )
            move = client.post(
                f"/messages/{ids['message']}/action",
                json={
                    "action": "move", "target_folder_id": str(ids["target"]),
                    "idempotency_key": key,
                },
            )

        assert flag.status_code == 200, flag.text
        assert move.status_code == 409, move.text
        assert client.portal.call(_folder_of, migrated_db, ids["message"]) == ids["source"]

    def test_a_failed_request_leaves_its_key_unused(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """A 4xx did nothing, so there is nothing to replay -- the key's claim
        is released rather than answering every retry with the old error."""
        ids = client.portal.call(_setup, migrated_db)
        key = str(uuid.uuid4())
        with patch(_MAILS_TARGET, return_value=migrated_db):
            resp = client.post(
                f"/messages/{ids['message']}/action",
                json={"action": "move", "target_folder_id": str(uuid.uuid4()),
                      "idempotency_key": key},
            )

        assert resp.status_code == 400, resp.text
        assert client.portal.call(_submission_count, migrated_db, key) == 0

    def test_a_repeat_while_the_first_is_still_running_is_told_to_retry(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        ids = client.portal.call(_setup, migrated_db)
        key = str(uuid.uuid4())
        body = {"action": "move", "target_folder_id": str(ids["target"]), "idempotency_key": key}
        with patch(_MAILS_TARGET, return_value=migrated_db):
            first = client.post(f"/messages/{ids['message']}/action", json=body)
            client.portal.call(_put_back, migrated_db, ids["message"], ids["source"])
            client.portal.call(_reopen_claim, migrated_db, key, timedelta(0))
            second = client.post(f"/messages/{ids['message']}/action", json=body)

        assert first.status_code == 200, first.text
        assert second.status_code == 503, second.text
        assert second.headers.get("retry-after") == "1"
        assert client.portal.call(_folder_of, migrated_db, ids["message"]) == ids["source"]

    def test_an_abandoned_claim_is_taken_over(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """A process killed mid-action leaves a claim with no response; a
        retry long after it applies the action rather than waiting forever."""
        ids = client.portal.call(_setup, migrated_db)
        key = str(uuid.uuid4())
        body = {"action": "move", "target_folder_id": str(ids["target"]), "idempotency_key": key}
        with patch(_MAILS_TARGET, return_value=migrated_db):
            client.post(f"/messages/{ids['message']}/action", json=body)
            client.portal.call(_put_back, migrated_db, ids["message"], ids["source"])
            client.portal.call(_reopen_claim, migrated_db, key, timedelta(hours=1))
            retry = client.post(f"/messages/{ids['message']}/action", json=body)

        assert retry.status_code == 200, retry.text
        assert client.portal.call(_folder_of, migrated_db, ids["message"]) == ids["target"]


async def _reopen_claim(migrated_db: DatabaseConnection, key: str, age: timedelta) -> None:
    """Turn a finished submission back into a claim of the given age -- the
    state a request still running, or one killed mid-action, leaves."""
    async with migrated_db.session() as session:
        await session.execute(
            text(
                "UPDATE message_action_submissions SET response = NULL, completed_at = NULL, "
                "created_at = now() - make_interval(secs => :age) WHERE idempotency_key = :key"
            ),
            {"age": age.total_seconds(), "key": uuid.UUID(key)},
        )


class TestBulkAction:
    def test_the_same_key_twice_moves_once_and_replays_the_response(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        ids = client.portal.call(_setup, migrated_db)
        body = {
            "action": "move", "target_folder_id": str(ids["target"]),
            "ids": [str(ids["message"])], "idempotency_key": str(uuid.uuid4()),
        }
        path = f"/accounts/{ids['account']}/messages/bulk-action"
        with patch(_MAILS_TARGET, return_value=migrated_db):
            first = client.post(path, json=body)
            assert first.status_code == 200, first.text
            assert first.json()["affected_count"] == 1
            client.portal.call(_put_back, migrated_db, ids["message"], ids["source"])
            second = client.post(path, json=body)

        assert second.status_code == 200, second.text
        assert second.json() == first.json()
        assert client.portal.call(_folder_of, migrated_db, ids["message"]) == ids["source"]

    def test_an_unsuccessful_response_is_not_replayed(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """Bulk answers 200 with success=false when it did nothing (no Archive
        folder, here) -- that leaves the key unused, like an error does."""
        ids = client.portal.call(_setup, migrated_db)
        key = str(uuid.uuid4())
        path = f"/accounts/{ids['account']}/messages/bulk-action"
        with patch(_MAILS_TARGET, return_value=migrated_db):
            resp = client.post(
                path,
                json={"action": "archive", "ids": [str(ids["message"])], "idempotency_key": key},
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is False
        assert client.portal.call(_submission_count, migrated_db, key) == 0


class TestPruning:
    @pytest.mark.asyncio
    async def test_only_submissions_past_retention_are_pruned(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        old_key, new_key = uuid.uuid4(), uuid.uuid4()
        async with migrated_db.session() as session:
            session.add_all([
                MessageActionSubmission(idempotency_key=old_key, fingerprint="a", response={}),
                MessageActionSubmission(idempotency_key=new_key, fingerprint="b", response={}),
            ])
        async with migrated_db.session() as session:
            await session.execute(
                text(
                    "UPDATE message_action_submissions SET created_at = now() - interval '8 days' "
                    "WHERE idempotency_key = :key"
                ),
                {"key": old_key},
            )

        await prune_submissions_once(migrated_db, timedelta(days=7))

        async with migrated_db.session() as session:
            remaining = set(
                (await session.scalars(
                    select(MessageActionSubmission.idempotency_key).where(
                        MessageActionSubmission.idempotency_key.in_([old_key, new_key]),
                    )
                )).all()
            )
        assert remaining == {new_key}
