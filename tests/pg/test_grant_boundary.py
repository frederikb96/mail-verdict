"""
The actual write boundary: connected as postimap_app, not the Postgres owner.

Every other pg test connects as the database owner, so a write to a column
outside the consumer contract would pass there regardless of whether the
grant actually permits it. These tests run through the real grant instead:
a permitted write still works, and a write the contract does not grant --
even one that never reaches production code, a raw UPDATE on a read-only
column -- is refused by Postgres itself with permission denied.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Message
from mail_verdict.postimap.actions import set_flags


async def _seed_account_folder_message(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert a minimal account/folder/message chain via raw SQL, return their ids.

    Runs against migrated_db (the owner connection) -- the restricted role
    has no INSERT grant on any of these tables, which is exactly the
    boundary under test, so seeding can never go through it.
    """
    account_id = uuid.uuid4()
    folder_id = uuid.uuid4()
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
        {"id": folder_id, "account_id": account_id},
    )
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject) "
            "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :message_id, 'Original subject')"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": folder_id,
            "thread_id": uuid.uuid4(), "message_id": f"<{message_id}@example.com>",
        },
    )
    return account_id, folder_id, message_id


@pytest.mark.asyncio
async def test_set_flags_succeeds_under_the_restricted_grant(
    migrated_db: DatabaseConnection, restricted_db: DatabaseConnection,
) -> None:
    """set_flags writes only is_seen -- a column postimap_app actually grants.

    The positive control: without this, a suite that only ever exercises
    the negative case could not tell "the role is scoped correctly" apart
    from "the role can write nothing at all".
    """
    async with migrated_db.session() as session:
        _account_id, _folder_id, message_id = await _seed_account_folder_message(session)
        await session.commit()

    async with restricted_db.session() as session:
        await set_flags(session, message_id, is_seen=True)
        await session.commit()

    async with migrated_db.session() as session:
        result = await session.execute(select(Message.is_seen).where(Message.id == message_id))
        assert result.scalar_one() is True


@pytest.mark.asyncio
async def test_writing_a_read_only_column_is_denied(
    migrated_db: DatabaseConnection, restricted_db: DatabaseConnection,
) -> None:
    """subject is read-only per the contract -- the grant enforces it, not just the doc.

    This never goes through postimap/actions.py (nothing there writes
    subject); it proves the boundary exists at the database level even for
    a write our own code never attempts, which is the actual safety net if
    that ever changes by mistake.
    """
    async with migrated_db.session() as session:
        _account_id, _folder_id, message_id = await _seed_account_folder_message(session)
        await session.commit()

    with pytest.raises(DBAPIError) as exc_info:
        async with restricted_db.session() as session:
            await session.execute(
                update(Message).where(Message.id == message_id).values(subject="tampered")
            )
            await session.commit()

    assert "permission denied" in str(exc_info.value).lower()

    async with migrated_db.session() as session:
        result = await session.execute(select(Message.subject).where(Message.id == message_id))
        assert result.scalar_one() == "Original subject"


@pytest.mark.asyncio
async def test_insert_into_messages_is_denied(
    migrated_db: DatabaseConnection, restricted_db: DatabaseConnection,
) -> None:
    """messages has no INSERT grant at all -- a row exists because it exists on IMAP.

    Postgres checks table-level INSERT privilege before evaluating any
    constraint, so this fails on the grant itself rather than on the
    made-up foreign keys below ever being validated.
    """
    with pytest.raises(DBAPIError) as exc_info:
        async with restricted_db.session() as session:
            await session.execute(
                text(
                    "INSERT INTO messages (id, account_id, folder_id, thread_id, message_id) "
                    "VALUES (:id, :account_id, :folder_id, :thread_id, :msg_id)"
                ),
                {
                    "id": uuid.uuid4(), "account_id": uuid.uuid4(), "folder_id": uuid.uuid4(),
                    "thread_id": uuid.uuid4(), "msg_id": "<denied@example.com>",
                },
            )
            await session.commit()

    assert "permission denied" in str(exc_info.value).lower()
