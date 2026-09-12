"""
GET /messages/{id}?load_images=true against a real database -- whether
the per-message override actually restores remote images for a sender
the account has never allowlisted, not only for one it already trusts.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from mail_verdict.api.mails import router
from mail_verdict.database.connection import DatabaseConnection
from tests.pg.test_bulk_actions_and_outbox import _seed_account_two_folders

_REMOTE_IMG = '<img src="https://tracker.example.com/pixel.png" alt="">'


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


async def _seed_message_from_untrusted_sender(
    migrated_db: DatabaseConnection,
) -> uuid.UUID:
    """One message whose sender the account has never allowlisted, with a
    remote image in its body -- GET .../messages/{id} strips it by
    default."""
    async with migrated_db.session() as session:
        account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
        message_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, account_id, folder_id, imap_uid, thread_id, message_id, "
                "from_addr, body_html) "
                "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :msg_id, "
                ":from_addr, :body_html)"
            ),
            {
                "id": message_id, "account_id": account_id, "folder_id": inbox_id,
                "thread_id": uuid.uuid4(), "msg_id": f"<{message_id}@example.com>",
                "from_addr": "newsletter@untrusted.example.com",
                "body_html": f"<p>Hello</p>{_REMOTE_IMG}",
            },
        )
        await session.commit()
    return message_id


class TestLoadImagesOverride:
    def test_default_strips_remote_images_from_an_unallowlisted_sender(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        message_id = client.portal.call(_seed_message_from_untrusted_sender, migrated_db)

        with patch("mail_verdict.api.mails.get_db_connection", return_value=migrated_db):
            resp = client.get(f"/messages/{message_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["images_allowed"] is False
        assert body["has_blocked_images"] is True
        assert "tracker.example.com" not in body["body_html"]

    def test_load_images_true_restores_them_for_that_one_request(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        message_id = client.portal.call(_seed_message_from_untrusted_sender, migrated_db)

        with patch("mail_verdict.api.mails.get_db_connection", return_value=migrated_db):
            resp = client.get(f"/messages/{message_id}", params={"load_images": "true"})
        assert resp.status_code == 200
        body = resp.json()
        # The sender's durable allowlist status is unchanged by a one-off
        # override -- only this response's own body/banner state reflects it.
        assert body["images_allowed"] is False
        assert body["has_blocked_images"] is False
        assert "tracker.example.com" in body["body_html"]

        # And the override really was one-off: a plain re-fetch goes back
        # to stripping, rather than the sender having been silently trusted.
        with patch("mail_verdict.api.mails.get_db_connection", return_value=migrated_db):
            again = client.get(f"/messages/{message_id}")
        assert again.json()["has_blocked_images"] is True
