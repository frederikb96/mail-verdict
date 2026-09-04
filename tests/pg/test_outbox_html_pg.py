"""
The outbound HTML path against a real database: a compose POST stores
sanitised body_html, refuses HTML with no text alternative, and a
message's own quote endpoint turns its raw body_html/body_text into the
same safe-to-send shape.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.mails import router as mails_router
from mail_verdict.api.outbox import router as outbox_router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Outbox


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(outbox_router)
    app.include_router(mails_router)
    with TestClient(app) as c:
        yield c


async def _insert_account(session: AsyncSession) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    return account_id


async def _seed_account(migrated_db: DatabaseConnection) -> uuid.UUID:
    async with migrated_db.session() as session:
        account_id = await _insert_account(session)
        await session.commit()
    return account_id


async def _seed_account_with_message(
    migrated_db: DatabaseConnection, body_html: str | None, body_text: str | None,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with migrated_db.session() as session:
        account_id = await _insert_account(session)
        folder_id = uuid.uuid4()
        message_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO folders (id, account_id, imap_name, special_use) "
                "VALUES (:id, :account_id, 'INBOX', NULL)"
            ),
            {"id": folder_id, "account_id": account_id},
        )
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
                "body_html, body_text) "
                "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :message_id, 'Original', "
                ":body_html, :body_text)"
            ),
            {
                "id": message_id, "account_id": account_id, "folder_id": folder_id,
                "thread_id": uuid.uuid4(), "message_id": f"<{message_id}@example.com>",
                "body_html": body_html, "body_text": body_text,
            },
        )
        await session.commit()
    return account_id, message_id


async def _outbox_body_html(migrated_db: DatabaseConnection, outbox_id: uuid.UUID) -> str | None:
    async with migrated_db.session() as session:
        return await session.scalar(select(Outbox.body_html).where(Outbox.id == outbox_id))


_OUTBOX_TARGET = "mail_verdict.api.outbox.get_db_connection"
_MAILS_TARGET = "mail_verdict.api.mails.get_db_connection"


class TestOutboxHtmlSanitisation:
    def test_body_html_is_sanitised_before_it_reaches_the_row(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = client.portal.call(_seed_account, migrated_db)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            resp = client.post(
                "/outbox",
                json={
                    "account_id": str(account_id), "kind": "draft",
                    "to": ["them@example.com"], "subject": "hi",
                    "body_text": "hi",
                    "body_html": '<p class="x" onclick="y()">hi</p><script>bad()</script>',
                },
            )
        assert resp.status_code == 201, resp.text
        stored = client.portal.call(_outbox_body_html, migrated_db, uuid.UUID(resp.json()["id"]))
        assert stored is not None
        assert "class=" not in stored
        assert "onclick" not in stored
        assert "<script" not in stored
        assert "hi" in stored

    def test_body_html_without_body_text_is_refused(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = client.portal.call(_seed_account, migrated_db)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            resp = client.post(
                "/outbox",
                json={
                    "account_id": str(account_id), "kind": "draft",
                    "to": ["them@example.com"], "subject": "hi",
                    "body_html": "<p>hi</p>",
                },
            )
        assert resp.status_code == 400, resp.text

    def test_plain_text_only_is_unaffected(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """A producer that never adopts body_html -- MCP's send/draft
        tools among them -- behaves exactly as before this path existed."""
        account_id = client.portal.call(_seed_account, migrated_db)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            resp = client.post(
                "/outbox",
                json={
                    "account_id": str(account_id), "kind": "draft",
                    "to": ["them@example.com"], "subject": "hi", "body_text": "hi",
                },
            )
        assert resp.status_code == 201, resp.text
        stored = client.portal.call(_outbox_body_html, migrated_db, uuid.UUID(resp.json()["id"]))
        assert stored is None


class TestMessageQuote:
    def test_quotes_the_raw_html_body_sanitised(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        html = '<p style="color:red">hello</p><script>bad()</script>'
        _, message_id = client.portal.call(
            _seed_account_with_message, migrated_db, html, "hello",
        )
        with patch(_MAILS_TARGET, return_value=migrated_db):
            resp = client.get(f"/messages/{message_id}/quote")
        assert resp.status_code == 200, resp.text
        out = resp.json()["html"]
        assert "hello" in out
        assert "<script" not in out
        assert "color:red" not in out

    def test_a_remote_image_quotes_as_its_own_absolute_url(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        html = '<p>see</p><img src="https://sender.example/pic.png">'
        _, message_id = client.portal.call(
            _seed_account_with_message, migrated_db, html, "see",
        )
        with patch(_MAILS_TARGET, return_value=migrated_db):
            resp = client.get(f"/messages/{message_id}/quote")
        assert 'src="https://sender.example/pic.png"' in resp.json()["html"]

    def test_a_cid_image_is_dropped_since_nothing_is_attached_to_the_quote(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        html = '<p>see</p><img src="cid:abc123">'
        _, message_id = client.portal.call(
            _seed_account_with_message, migrated_db, html, "see",
        )
        with patch(_MAILS_TARGET, return_value=migrated_db):
            resp = client.get(f"/messages/{message_id}/quote")
        assert "<img" not in resp.json()["html"]

    def test_a_text_only_message_quotes_as_escaped_html(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        _, message_id = client.portal.call(
            _seed_account_with_message, migrated_db, None, "line one\nline two <b>not bold</b>",
        )
        with patch(_MAILS_TARGET, return_value=migrated_db):
            resp = client.get(f"/messages/{message_id}/quote")
        out = resp.json()["html"]
        assert "line one<br>line two" in out
        assert "&lt;b&gt;" in out

    def test_unknown_message_is_a_404(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        with patch(_MAILS_TARGET, return_value=migrated_db):
            resp = client.get(f"/messages/{uuid.uuid4()}/quote")
        assert resp.status_code == 404
