"""
VerdictRepository.create_verdict against a real messages table -- proving
msg_key and from_addr are derived correctly for both the header and the
hash-fallback path, and that the durability gate they back is enforced by
the schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import VerdictSource
from mail_verdict.database.msg_key import compute_msg_key
from mail_verdict.database.repository import VerdictRepository


async def _seed_message(
    session: AsyncSession,
    *,
    message_id_hdr: str | None,
    from_addr: str = "sender@example.com",
    subject: str = "Hello",
    received_at: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc),
    size_bytes: int = 1024,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a minimal account + message row, return (account_id, message_id)."""
    account_id = uuid.uuid4()
    folder_id = uuid.uuid4()
    mail_id = uuid.uuid4()

    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text("INSERT INTO folders (id, account_id, imap_name) VALUES (:id, :account_id, 'INBOX')"),
        {"id": folder_id, "account_id": account_id},
    )
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, from_addr, "
            "subject, received_at, size_bytes) "
            "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :message_id, :from_addr, "
            ":subject, :received_at, :size_bytes)"
        ),
        {
            "id": mail_id, "account_id": account_id, "folder_id": folder_id,
            "thread_id": uuid.uuid4(), "message_id": message_id_hdr, "from_addr": from_addr,
            "subject": subject, "received_at": received_at, "size_bytes": size_bytes,
        },
    )
    return account_id, mail_id


@pytest.mark.asyncio
async def test_create_verdict_uses_the_header_as_msg_key(
    migrated_db: DatabaseConnection,
) -> None:
    """A message with a Message-ID header gets that header as its msg_key, and its
    from_addr copied onto the verdict -- both without the caller passing them."""
    repo = VerdictRepository(migrated_db)
    header = f"<{uuid.uuid4()}@example.com>"

    async with migrated_db.session() as session:
        account_id, mail_id = await _seed_message(session, message_id_hdr=header)
        await session.commit()

    verdict = await repo.create_verdict(
        mail_id=mail_id, account_id=account_id, is_spam=True,
        source=VerdictSource.AI, message_id_hdr=header,
    )

    assert verdict.msg_key == header
    assert verdict.from_addr == "sender@example.com"


@pytest.mark.asyncio
async def test_create_verdict_falls_back_to_a_hash_with_no_header(
    migrated_db: DatabaseConnection,
) -> None:
    """A message with no Message-ID header still gets a durable, non-null msg_key --
    the hole this closes: such a message used to skip the durability gate outright."""
    repo = VerdictRepository(migrated_db)

    async with migrated_db.session() as session:
        account_id, mail_id = await _seed_message(session, message_id_hdr=None)
        await session.commit()

    verdict = await repo.create_verdict(
        mail_id=mail_id, account_id=account_id, is_spam=True, source=VerdictSource.AI,
    )

    expected = compute_msg_key(
        account_id=account_id, message_id_hdr=None, from_addr="sender@example.com",
        subject="Hello", received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        size_bytes=1024,
    )
    assert verdict.msg_key == expected
    assert verdict.msg_key.startswith("sha256:")


@pytest.mark.asyncio
async def test_a_second_verdict_via_create_verdict_is_rejected(
    migrated_db: DatabaseConnection,
) -> None:
    """The gate holds end to end through the repository method, not only when a
    Verdict row is constructed by hand: a resync producing a second AI verdict
    for the same message is rejected at the database."""
    repo = VerdictRepository(migrated_db)
    header = f"<{uuid.uuid4()}@example.com>"

    async with migrated_db.session() as session:
        account_id, mail_id = await _seed_message(session, message_id_hdr=header)
        await session.commit()

    await repo.create_verdict(
        mail_id=mail_id, account_id=account_id, is_spam=False,
        source=VerdictSource.AI, message_id_hdr=header,
    )

    with pytest.raises(IntegrityError):
        await repo.create_verdict(
            mail_id=mail_id, account_id=account_id, is_spam=True,
            source=VerdictSource.AI, message_id_hdr=header,
        )
