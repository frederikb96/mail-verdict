"""
GET /messages/:id/raw (the .eml download) and non-Latin-1 attachment
filenames, against a real Postgres schema.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.mails import get_attachment, get_raw_source
from mail_verdict.database.connection import DatabaseConnection


async def _seed_account_inbox_and_message(
    session: AsyncSession,
    *,
    raw_source: bytes | None,
    is_truncated: bool = False,
    subject: str = "Test message",
) -> tuple[uuid.UUID, uuid.UUID]:
    account_id = uuid.uuid4()
    inbox_id = uuid.uuid4()
    message_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use) "
            "VALUES (:id, :account_id, 'INBOX', NULL)"
        ),
        {"id": inbox_id, "account_id": account_id},
    )
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
            " raw_source, is_truncated) "
            "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :msg_id, :subject, "
            " :raw_source, :is_truncated)"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": inbox_id,
            "thread_id": message_id, "msg_id": f"<{message_id}@example.com>",
            "subject": subject, "raw_source": raw_source, "is_truncated": is_truncated,
        },
    )
    return account_id, message_id


@pytest.mark.asyncio
async def test_raw_source_download_returns_the_stored_bytes(
    migrated_db: DatabaseConnection,
) -> None:
    raw_bytes = b"From: a@example.com\r\nSubject: Test message\r\n\r\nBody\r\n"
    async with migrated_db.session() as session:
        _, message_id = await _seed_account_inbox_and_message(session, raw_source=raw_bytes)

    response = await get_raw_source(message_id)

    assert response.media_type == "message/rfc822"
    assert response.body == raw_bytes
    assert "Test message.eml" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_raw_source_missing_because_truncated_is_409_not_404(
    migrated_db: DatabaseConnection,
) -> None:
    """raw_source is NULL when the message exceeded storage.max_message_bytes
    and was never fetched -- distinct from the message not existing at all."""
    async with migrated_db.session() as session:
        _, message_id = await _seed_account_inbox_and_message(
            session, raw_source=None, is_truncated=True,
        )

    with pytest.raises(HTTPException) as exc_info:
        await get_raw_source(message_id)

    assert exc_info.value.status_code == 409
    assert "storage.max_message_bytes" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_raw_source_unknown_message_is_404(migrated_db: DatabaseConnection) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await get_raw_source(uuid.uuid4())

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_attachment_download_with_non_latin1_filename_does_not_raise(
    migrated_db: DatabaseConnection,
) -> None:
    """The bug this guards: a plain filename="..." header raises
    UnicodeEncodeError building the response for any filename outside
    Latin-1, turning a download into a 500."""
    async with migrated_db.session() as session:
        _, message_id = await _seed_account_inbox_and_message(session, raw_source=None)
        attachment_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO attachments (id, message_id, filename, content_type, data) "
                "VALUES (:id, :message_id, :filename, 'application/pdf', :data)"
            ),
            {
                "id": attachment_id, "message_id": message_id,
                "filename": "请假条_😀.pdf", "data": b"%PDF-1.4 fake",
            },
        )

    response = await get_attachment(message_id, attachment_id)

    assert response.body == b"%PDF-1.4 fake"
    header = response.headers["content-disposition"]
    header.encode("latin-1")  # would already have raised at Response() construction
    assert "filename*=UTF-8''" in header
