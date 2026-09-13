"""
GZip response compression against real attachment and raw-source
downloads: binary and byte-for-byte content the compressor must never
corrupt, whether or not it actually compresses them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from functools import partial
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.gzip import GZipMiddleware

from mail_verdict.api.mails import router as mails_router
from mail_verdict.database.connection import DatabaseConnection

_MAILS_TARGET = "mail_verdict.api.mails.get_db_connection"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """The same minimal app test_outbox_html_pg.py builds, with the
    production GZipMiddleware added -- the setting under test, not the
    default Starlette carries without it."""
    app = FastAPI()
    app.include_router(mails_router)
    app.add_middleware(GZipMiddleware, minimum_size=500)
    with TestClient(app) as c:
        yield c


async def _seed_message_with_attachment(
    migrated_db: DatabaseConnection, *, raw_source: bytes, attachment_data: bytes,
) -> tuple[uuid.UUID, uuid.UUID]:
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
        message_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
                " raw_source) "
                "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :msg_id, 'Test', "
                " :raw_source)"
            ),
            {
                "id": message_id, "account_id": account_id, "folder_id": folder_id,
                "thread_id": message_id, "msg_id": f"<{message_id}@example.com>",
                "raw_source": raw_source,
            },
        )
        attachment_id = await _attach(session, message_id, attachment_data)
        await session.commit()
    return message_id, attachment_id


async def _attach(session: AsyncSession, message_id: uuid.UUID, data: bytes) -> uuid.UUID:
    attachment_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO attachments (id, message_id, filename, content_type, data) "
            "VALUES (:id, :message_id, 'report.pdf', 'application/pdf', :data)"
        ),
        {"id": attachment_id, "message_id": message_id, "data": data},
    )
    return attachment_id


def test_attachment_bytes_round_trip_exactly_under_gzip(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    # Comfortably over minimum_size and covering every byte value, so a
    # corrupting compressor is exercised on real binary content rather
    # than sidestepped by a body too small to be compressed at all.
    attachment_data = bytes(range(256)) * 20  # 5120 bytes
    raw_source = b"From: a@example.com\r\nSubject: Test\r\n\r\n" + attachment_data
    message_id, attachment_id = client.portal.call(
        partial(
            _seed_message_with_attachment,
            migrated_db, raw_source=raw_source, attachment_data=attachment_data,
        )
    )

    with patch(_MAILS_TARGET, return_value=migrated_db):
        att_resp = client.get(
            f"/messages/{message_id}/attachments/{attachment_id}",
            headers={"Accept-Encoding": "gzip"},
        )
        raw_resp = client.get(
            f"/messages/{message_id}/raw", headers={"Accept-Encoding": "gzip"},
        )

    assert att_resp.status_code == 200, att_resp.text
    # Confirms compression was actually attempted here, not merely absent
    # for an unrelated reason (too small, wrong content type) that would
    # make the round-trip check below pass without proving anything.
    assert att_resp.headers.get("content-encoding") == "gzip"
    assert att_resp.content == attachment_data

    assert raw_resp.status_code == 200, raw_resp.text
    assert raw_resp.headers.get("content-encoding") == "gzip"
    assert raw_resp.content == raw_source
