"""
get_thread and get_message batch their tags/attachments/verdicts and their
image allowlist across every message in the conversation in a fixed number
of queries, rather than one round trip per message per concern. A batched
mapping keyed by the wrong id, or built from an unfiltered fetch, would
leak one message's attachment, verdict or tag onto another -- this seeds a
two-message thread where every one of those differs per message and checks
each row gets its own.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.mails import router as mails_router
from mail_verdict.database.connection import DatabaseConnection


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(mails_router)
    with TestClient(app) as c:
        yield c


async def _seed_thread(migrated_db: DatabaseConnection) -> dict[str, uuid.UUID]:
    """
    A two-message thread, one older and one newer, each with its own
    sender, attachment, verdict and tag -- and only the older sender
    allowlisted, so images_allowed must differ between the two rows too.
    """
    async with migrated_db.session() as session:
        account_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, "
                "imap_password) VALUES (:id, :name, 'imap.example.com', 993, "
                "'user@example.com', '\\x00' || convert_to('pw', 'UTF8'))"
            ),
            {"id": account_id, "name": f"acct-{account_id}"},
        )
        folder_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO folders (id, account_id, imap_name, special_use) "
                "VALUES (:id, :account_id, 'INBOX', NULL)"
            ),
            {"id": folder_id, "account_id": account_id},
        )

        thread_id = uuid.uuid4()
        older_id, newer_id = uuid.uuid4(), uuid.uuid4()
        for msg_id, uid, from_addr, subject, sent_html in (
            (older_id, 1, "allowed@sender.example", "Older", '<img src="https://a.example/x.png">'),
            (newer_id, 2, "blocked@sender.example", "Newer", '<img src="https://b.example/y.png">'),
        ):
            await session.execute(
                text(
                    "INSERT INTO messages (id, account_id, folder_id, imap_uid, thread_id, "
                    "message_id, subject, from_addr, body_html, body_text, received_at) "
                    "VALUES (:id, :account_id, :folder_id, :uid, :thread_id, :message_id, "
                    ":subject, :from_addr, :body_html, 'body', now())"
                ),
                {
                    "id": msg_id, "account_id": account_id, "folder_id": folder_id, "uid": uid,
                    "thread_id": thread_id, "message_id": f"<{msg_id}@example.com>",
                    "subject": subject, "from_addr": from_addr, "body_html": sent_html,
                },
            )

        await session.execute(
            text(
                "INSERT INTO image_exceptions (id, account_id, exception_type, value) "
                "VALUES (:id, :account_id, 'sender', 'allowed@sender.example')"
            ),
            {"id": uuid.uuid4(), "account_id": account_id},
        )

        await _attach(session, older_id, "older.pdf")
        await _attach(session, newer_id, "newer.pdf")

        await _verdict(session, older_id, account_id, is_spam=False)
        await _verdict(session, newer_id, account_id, is_spam=True)

        await _tag(session, older_id, "older-tag")
        await _tag(session, newer_id, "newer-tag")

        await session.commit()
    return {"older_id": older_id, "newer_id": newer_id}


async def _attach(session: AsyncSession, message_id: uuid.UUID, filename: str) -> None:
    await session.execute(
        text(
            "INSERT INTO attachments (id, message_id, filename, content_type, data) "
            "VALUES (:id, :message_id, :filename, 'application/pdf', :data)"
        ),
        {"id": uuid.uuid4(), "message_id": message_id, "filename": filename, "data": b"%PDF"},
    )


async def _verdict(
    session: AsyncSession, mail_id: uuid.UUID, account_id: uuid.UUID, *, is_spam: bool,
) -> None:
    await session.execute(
        text(
            "INSERT INTO verdicts (id, mail_id, account_id, msg_key, is_spam, source) "
            "VALUES (:id, :mail_id, :account_id, :msg_key, :is_spam, 'ai')"
        ),
        {
            "id": uuid.uuid4(), "mail_id": mail_id, "account_id": account_id,
            "msg_key": f"k-{mail_id}", "is_spam": is_spam,
        },
    )


async def _tag(session: AsyncSession, mail_id: uuid.UUID, tag_name: str) -> None:
    await session.execute(
        text(
            "INSERT INTO mail_tags (id, mail_id, tag_name, source) "
            "VALUES (:id, :mail_id, :tag_name, 'user')"
        ),
        {"id": uuid.uuid4(), "mail_id": mail_id, "tag_name": tag_name},
    )


def test_each_thread_row_carries_its_own_attachment_verdict_tag_and_images_allowed(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    seeded = client.portal.call(_seed_thread, migrated_db)
    resp = client.get(f"/messages/{seeded['older_id']}/thread")
    assert resp.status_code == 200, resp.text
    by_id = {m["id"]: m for m in resp.json()["messages"]}
    assert set(by_id) == {str(seeded["older_id"]), str(seeded["newer_id"])}

    older = by_id[str(seeded["older_id"])]
    newer = by_id[str(seeded["newer_id"])]

    assert [a["filename"] for a in older["attachments"]] == ["older.pdf"]
    assert [a["filename"] for a in newer["attachments"]] == ["newer.pdf"]

    assert older["verdict"]["is_spam"] is False
    assert newer["verdict"]["is_spam"] is True

    assert [t["tag_name"] for t in older["tags"]] == ["older-tag"]
    assert [t["tag_name"] for t in newer["tags"]] == ["newer-tag"]

    assert older["images_allowed"] is True
    assert newer["images_allowed"] is False


def test_get_message_agrees_with_get_thread_for_the_same_row(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    """get_message shares the same batched lookup shape (single-id lists) --
    this is what would catch that path indexing into an empty dict or the
    wrong key now that it no longer opens one session per repository call."""
    seeded = client.portal.call(_seed_thread, migrated_db)
    resp = client.get(f"/messages/{seeded['newer_id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [a["filename"] for a in body["attachments"]] == ["newer.pdf"]
    assert body["verdict"]["is_spam"] is True
    assert [t["tag_name"] for t in body["tags"]] == ["newer-tag"]
    assert body["images_allowed"] is False
