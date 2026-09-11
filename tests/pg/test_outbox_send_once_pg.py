"""
A message is sent once, however many times the request that sends it
arrives: a repeated POST /outbox carrying the same idempotency_key
collapses onto the first one, and a draft already on its way out cannot
be sent a second time. Both hold against a real database, through the
same endpoint the composer calls, whatever the client did or failed to do.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.outbox import router as outbox_router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Outbox, PendingSend
from mail_verdict.settings.service import init_settings_service, reset_settings_service

_OUTBOX_TARGET = "mail_verdict.api.outbox.get_db_connection"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(outbox_router)
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_settings_after() -> Iterator[None]:
    yield
    reset_settings_service()


async def _seed_account_with_draft(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """An inactive account -- PostIMAP never picks up an inactive account's
    outbox, so every row these tests write stays exactly as written -- with
    one draft in its Drafts folder to send from."""
    account_id = uuid.uuid4()
    drafts_id = uuid.uuid4()
    draft_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password, "
            "is_active) VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'), false)"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use) "
            "VALUES (:id, :account_id, 'Drafts', 'drafts')"
        ),
        {"id": drafts_id, "account_id": account_id},
    )
    await session.execute(
        text(
            "INSERT INTO messages (id, account_id, folder_id, imap_uid, thread_id, message_id, "
            "subject, is_draft, received_at) VALUES (:id, :account_id, :folder_id, 1, "
            ":thread_id, :msg_id, 'A draft', true, now())"
        ),
        {
            "id": draft_id, "account_id": account_id, "folder_id": drafts_id,
            "thread_id": uuid.uuid4(), "msg_id": f"<{draft_id}@example.com>",
        },
    )
    return account_id, draft_id


async def _setup(
    migrated_db: DatabaseConnection, undo_send_seconds: float,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with migrated_db.session() as session:
        ids = await _seed_account_with_draft(session)
        await session.commit()
    settings_service = await init_settings_service(migrated_db)
    await settings_service.update("outbox", {"undo_send_seconds": undo_send_seconds})
    return ids


async def _count_sends(migrated_db: DatabaseConnection, account_id: uuid.UUID) -> int:
    """Every send this account has anywhere: still inside its undo window,
    or already handed to PostIMAP."""
    async with migrated_db.session() as session:
        staged = await session.scalar(
            select(func.count()).select_from(PendingSend).where(
                PendingSend.account_id == account_id, PendingSend.cancelled_at.is_(None),
            )
        )
        queued = await session.scalar(
            select(func.count()).select_from(Outbox).where(
                Outbox.account_id == account_id, Outbox.kind == "send",
            )
        )
    return int(staged or 0) + int(queued or 0)


def _send_body(account_id: uuid.UUID, **extra: object) -> dict[str, object]:
    return {
        "account_id": str(account_id), "kind": "send",
        "to": ["them@example.com"], "subject": "Once only", "body_text": "hi", **extra,
    }


class TestRepeatedRequestCollapses:
    @pytest.mark.parametrize("undo_send_seconds", [30.0, 0.0])
    def test_the_same_idempotency_key_twice_creates_one_send(
        self, client: TestClient, migrated_db: DatabaseConnection, undo_send_seconds: float,
    ) -> None:
        """Staged for an undo window or handed straight to PostIMAP, the
        second request returns the first one's row rather than a new one."""
        account_id, _draft_id = client.portal.call(_setup, migrated_db, undo_send_seconds)
        key = str(uuid.uuid4())
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            first = client.post("/outbox", json=_send_body(account_id, idempotency_key=key))
            second = client.post("/outbox", json=_send_body(account_id, idempotency_key=key))

        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        assert second.json()["id"] == first.json()["id"]
        assert client.portal.call(_count_sends, migrated_db, account_id) == 1

    def test_different_keys_are_different_sends(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """The key is what identifies a repeat -- two distinct messages with
        identical content are still two messages."""
        account_id, _draft_id = client.portal.call(_setup, migrated_db, 30.0)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            for _ in range(2):
                resp = client.post(
                    "/outbox", json=_send_body(account_id, idempotency_key=str(uuid.uuid4())),
                )
                assert resp.status_code == 201, resp.text

        assert client.portal.call(_count_sends, migrated_db, account_id) == 2

    def test_a_key_reused_for_a_different_kind_is_refused(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """A draft save and a send are never the same request -- collapsing
        a send onto an earlier draft save would silently not send it."""
        account_id, _draft_id = client.portal.call(_setup, migrated_db, 30.0)
        key = str(uuid.uuid4())
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            draft = client.post(
                "/outbox", json={**_send_body(account_id, idempotency_key=key), "kind": "draft"},
            )
            send = client.post("/outbox", json=_send_body(account_id, idempotency_key=key))

        assert draft.status_code == 201, draft.text
        assert send.status_code == 409, send.text
        assert client.portal.call(_count_sends, migrated_db, account_id) == 0


class TestADraftIsSentOnce:
    @pytest.mark.parametrize("undo_send_seconds", [30.0, 0.0])
    def test_sending_the_same_draft_twice_is_refused(
        self, client: TestClient, migrated_db: DatabaseConnection, undo_send_seconds: float,
    ) -> None:
        """The shape that actually went out twice: two sends naming the same
        draft, with nothing else in common -- no shared key, since a client
        that loses its own guard cannot be relied on to send one."""
        account_id, draft_id = client.portal.call(_setup, migrated_db, undo_send_seconds)
        body = _send_body(account_id, replaces_message_id=str(draft_id))
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            first = client.post("/outbox", json=body)
            second = client.post("/outbox", json=body)

        assert first.status_code == 201, first.text
        assert second.status_code == 409, second.text
        assert client.portal.call(_count_sends, migrated_db, account_id) == 1

    def test_a_draft_whose_send_was_undone_can_be_sent_again(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, draft_id = client.portal.call(_setup, migrated_db, 30.0)
        body = _send_body(account_id, replaces_message_id=str(draft_id))
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            first = client.post("/outbox", json=body)
            cancel = client.post(f"/outbox/pending/{first.json()['id']}/cancel")
            again = client.post("/outbox", json=body)

        assert cancel.status_code == 204, cancel.text
        assert again.status_code == 201, again.text
        assert client.portal.call(_count_sends, migrated_db, account_id) == 1

    def test_a_draft_whose_send_died_can_be_sent_again(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """A dead send never reached anyone and never removed the draft --
        sending it again is the only way forward, not a duplicate."""
        account_id, draft_id = client.portal.call(_setup, migrated_db, 0.0)
        body = _send_body(account_id, replaces_message_id=str(draft_id))
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            first = client.post("/outbox", json=body)

        async def _mark_dead() -> None:
            async with migrated_db.session() as session:
                await session.execute(
                    text("UPDATE outbox SET status = 'dead' WHERE id = :id"),
                    {"id": first.json()["id"]},
                )
                await session.commit()

        client.portal.call(_mark_dead)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            again = client.post("/outbox", json=body)

        assert again.status_code == 201, again.text

    def test_saving_the_draft_again_is_not_a_send(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """Only a send is refused -- two draft saves naming the same draft
        are an ordinary edit, left to PostIMAP's own supersede handling."""
        account_id, draft_id = client.portal.call(_setup, migrated_db, 30.0)
        body = {**_send_body(account_id, replaces_message_id=str(draft_id)), "kind": "draft"}
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            for _ in range(2):
                resp = client.post("/outbox", json=body)
                assert resp.status_code == 201, resp.text
