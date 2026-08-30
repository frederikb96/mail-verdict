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
from mail_verdict.database.models import Folder, Message, Outbox
from mail_verdict.postimap.actions import create_folder, delete_folder, insert_outbox, set_flags


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


@pytest.mark.asyncio
async def test_create_folder_succeeds_under_the_restricted_grant(
    migrated_db: DatabaseConnection, restricted_db: DatabaseConnection,
) -> None:
    """create_folder's Core INSERT names only account_id/imap_name -- exactly
    what postimap_app is granted -- and reads id back via RETURNING rather
    than sending a client-side one on the INSERT itself."""
    async with migrated_db.session() as session:
        account_id, _folder_id, _message_id = await _seed_account_folder_message(session)
        await session.commit()

    async with restricted_db.session() as session:
        new_folder_id = await create_folder(
            session, account_id=account_id, imap_name="Archive/2026",
        )
        await session.commit()

    async with migrated_db.session() as session:
        result = await session.execute(
            select(Folder.imap_name).where(Folder.id == new_folder_id)
        )
        assert result.scalar_one() == "Archive/2026"


@pytest.mark.asyncio
async def test_inserting_a_folder_id_directly_is_denied(
    migrated_db: DatabaseConnection, restricted_db: DatabaseConnection,
) -> None:
    """id carries no INSERT grant on folders -- naming it explicitly, the way
    an ORM-constructed row with a client-side default would, is refused
    rather than silently accepted."""
    async with migrated_db.session() as session:
        account_id, _folder_id, _message_id = await _seed_account_folder_message(session)
        await session.commit()

    with pytest.raises(DBAPIError) as exc_info:
        async with restricted_db.session() as session:
            await session.execute(
                text(
                    "INSERT INTO folders (id, account_id, imap_name) "
                    "VALUES (:id, :account_id, 'Denied')"
                ),
                {"id": uuid.uuid4(), "account_id": account_id},
            )
            await session.commit()

    assert "permission denied" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_delete_folder_succeeds_under_the_restricted_grant(
    migrated_db: DatabaseConnection, restricted_db: DatabaseConnection,
) -> None:
    """delete_folder writes only deleted_at -- the one UPDATE grant on folders."""
    async with migrated_db.session() as session:
        account_id, _inbox_id, _message_id = await _seed_account_folder_message(session)
        folder_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO folders (id, account_id, imap_name, special_use) "
                "VALUES (:id, :account_id, 'Archive', NULL)"
            ),
            {"id": folder_id, "account_id": account_id},
        )
        await session.commit()

    async with restricted_db.session() as session:
        await delete_folder(session, folder_id)
        await session.commit()

    async with migrated_db.session() as session:
        result = await session.execute(select(Folder.deleted_at).where(Folder.id == folder_id))
        assert result.scalar_one() is not None


@pytest.mark.asyncio
async def test_insert_outbox_with_replaces_message_id_succeeds_under_the_restricted_grant(
    migrated_db: DatabaseConnection, restricted_db: DatabaseConnection,
) -> None:
    """replaces_message_id is on outbox's column-level INSERT grant -- a draft
    edit must be writable through the real restricted role, not just the
    Postgres owner connection every other pg test uses."""
    async with migrated_db.session() as session:
        account_id, _inbox_id, message_id = await _seed_account_folder_message(session)
        await session.commit()

    async with restricted_db.session() as session:
        outbox = await insert_outbox(
            session, account_id=account_id, kind="draft",
            to_addrs=["them@example.com"], subject="Edited draft",
            body_text="Now finished.", replaces_message_id=message_id,
        )
        await session.commit()
        outbox_id = outbox.id

    async with migrated_db.session() as session:
        result = await session.execute(
            select(Outbox.replaces_message_id).where(Outbox.id == outbox_id)
        )
        assert result.scalar_one() == message_id
