"""
Bulk-write actions.py helpers and the outbox insert path, against a real
PostIMAP-migrated schema.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.mails import BulkActionScope, _resolve_scope_ids
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Message, Outbox, OutboxAttachment
from mail_verdict.postimap.actions import (
    expunge_bulk,
    insert_outbox,
    move_message_bulk,
    set_flags_bulk,
)


async def _seed_account_two_folders(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert an account with an inbox and a junk folder, return their ids."""
    account_id = uuid.uuid4()
    inbox_id = uuid.uuid4()
    junk_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    for folder_id, imap_name, special_use in ((inbox_id, "INBOX", None), (junk_id, "Junk", "junk")):
        await session.execute(
            text(
                "INSERT INTO folders (id, account_id, imap_name, special_use) "
                "VALUES (:id, :account_id, :imap_name, :special_use)"
            ),
            {
                "id": folder_id, "account_id": account_id,
                "imap_name": imap_name, "special_use": special_use,
            },
        )
    return account_id, inbox_id, junk_id


async def _seed_messages(
    session: AsyncSession,
    account_id: uuid.UUID,
    folder_id: uuid.UUID,
    count: int,
    *,
    is_seen: bool = False,
    uid_start: int = 1,
) -> list[uuid.UUID]:
    """Insert `count` bare messages into a folder, returning their ids.

    uid_start lets a test seed a second batch into the same folder without
    colliding with the UNIQUE(folder_id, imap_uid) constraint.
    """
    ids = []
    for i in range(count):
        message_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, account_id, folder_id, imap_uid, thread_id, message_id, is_seen) "
                "VALUES (:id, :account_id, :folder_id, :uid, :thread_id, :msg_id, :is_seen)"
            ),
            {
                "id": message_id, "account_id": account_id, "folder_id": folder_id,
                "uid": uid_start + i, "thread_id": uuid.uuid4(),
                "msg_id": f"<{message_id}@example.com>", "is_seen": is_seen,
            },
        )
        ids.append(message_id)
    return ids


class TestBulkWriteHelpers:
    """Tests for the batched contract-SQL helpers in postimap/actions.py."""

    @pytest.mark.asyncio
    async def test_set_flags_bulk_touches_every_id(self, migrated_db: DatabaseConnection) -> None:
        """A single UPDATE with an IN clause flips is_seen on every named message."""
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            ids = await _seed_messages(session, account_id, inbox_id, 3)
            await session.commit()

        async with migrated_db.session() as session:
            await set_flags_bulk(session, ids, is_seen=True)
            await session.commit()

        async with migrated_db.session() as session:
            result = await session.execute(select(Message.is_seen).where(Message.id.in_(ids)))
            assert all(seen for (seen,) in result.all())

    @pytest.mark.asyncio
    async def test_set_flags_bulk_empty_list_is_a_noop(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """An empty id list must not become an UPDATE with no WHERE clause."""
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            ids = await _seed_messages(session, account_id, inbox_id, 2, is_seen=False)
            await session.commit()

        async with migrated_db.session() as session:
            await set_flags_bulk(session, [], is_seen=True)
            await session.commit()

        async with migrated_db.session() as session:
            result = await session.execute(select(Message.is_seen).where(Message.id.in_(ids)))
            assert not any(seen for (seen,) in result.all())

    @pytest.mark.asyncio
    async def test_move_message_bulk_moves_every_id_and_nulls_uid(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The optimistic-move shape (folder_id + imap_uid=NULL) applies to every id."""
        async with migrated_db.session() as session:
            account_id, inbox_id, junk_id = await _seed_account_two_folders(session)
            ids = await _seed_messages(session, account_id, inbox_id, 3)
            await session.commit()

        async with migrated_db.session() as session:
            await move_message_bulk(session, ids, junk_id)
            await session.commit()

        async with migrated_db.session() as session:
            result = await session.execute(
                select(Message.folder_id, Message.imap_uid).where(Message.id.in_(ids))
            )
            rows = result.all()
            assert all(folder_id == junk_id for folder_id, _ in rows)
            assert all(uid is None for _, uid in rows)

    @pytest.mark.asyncio
    async def test_expunge_bulk_sets_timestamp_on_every_id(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """expunged_at is set on every named message; rows survive."""
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            ids = await _seed_messages(session, account_id, inbox_id, 2)
            await session.commit()

        async with migrated_db.session() as session:
            await expunge_bulk(session, ids)
            await session.commit()

        async with migrated_db.session() as session:
            result = await session.execute(select(Message.expunged_at).where(Message.id.in_(ids)))
            assert all(expunged_at is not None for (expunged_at,) in result.all())


class TestBulkActionScopeResolution:
    """Tests for resolving a 'select all matching' scope to concrete ids."""

    @pytest.mark.asyncio
    async def test_unread_filter_excludes_read_messages(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """filter='unread' resolves only to messages with is_seen=false."""
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            unread_ids = await _seed_messages(session, account_id, inbox_id, 2, is_seen=False)
            await _seed_messages(session, account_id, inbox_id, 2, is_seen=True, uid_start=3)
            await session.commit()

        async with migrated_db.session() as session:
            resolved = await _resolve_scope_ids(
                session, account_id, BulkActionScope(folder_id=inbox_id, filter="unread"),
            )

        assert set(resolved) == set(unread_ids)

    @pytest.mark.asyncio
    async def test_exclude_ids_removes_named_messages_from_all_filter(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """exclude_ids removes specific messages even from an otherwise-matching scope."""
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            ids = await _seed_messages(session, account_id, inbox_id, 3)
            await session.commit()

        excluded = ids[0]
        async with migrated_db.session() as session:
            resolved = await _resolve_scope_ids(
                session, account_id, BulkActionScope(folder_id=inbox_id, exclude_ids=[excluded]),
            )

        assert excluded not in resolved
        assert set(resolved) == set(ids) - {excluded}

    @pytest.mark.asyncio
    async def test_scope_never_crosses_folder_boundary(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """A scope for one folder must not resolve messages sitting in another."""
        async with migrated_db.session() as session:
            account_id, inbox_id, junk_id = await _seed_account_two_folders(session)
            inbox_ids = await _seed_messages(session, account_id, inbox_id, 2)
            await _seed_messages(session, account_id, junk_id, 2)
            await session.commit()

        async with migrated_db.session() as session:
            resolved = await _resolve_scope_ids(
                session, account_id, BulkActionScope(folder_id=inbox_id),
            )

        assert set(resolved) == set(inbox_ids)


class TestInsertOutbox:
    """Tests for postimap/actions.insert_outbox against the real outbox tables."""

    @pytest.mark.asyncio
    async def test_send_row_starts_pending_with_no_sent_at(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """A freshly inserted send row is pending; sent_at is set later by PostIMAP, not here."""
        async with migrated_db.session() as session:
            account_id, _inbox_id, _junk_id = await _seed_account_two_folders(session)
            outbox = await insert_outbox(
                session, account_id=account_id, kind="send",
                to_addrs=["them@example.com"], subject="Hello", body_text="Hi there.",
            )
            await session.commit()
            outbox_id = outbox.id

        async with migrated_db.session() as session:
            result = await session.execute(select(Outbox).where(Outbox.id == outbox_id))
            row = result.scalar_one()

        assert row.status == "pending"
        assert row.sent_at is None
        assert row.to_addrs == ["them@example.com"]

    @pytest.mark.asyncio
    async def test_attachments_land_in_outbox_attachments_before_pickup(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Attachments are inserted alongside the outbox row, not as a follow-up call."""
        async with migrated_db.session() as session:
            account_id, _inbox_id, _junk_id = await _seed_account_two_folders(session)
            outbox = await insert_outbox(
                session, account_id=account_id, kind="send",
                to_addrs=["them@example.com"], subject="Invoice", body_text="See attached.",
                attachments=[("invoice.pdf", "application/pdf", b"%PDF-1.4 fake")],
            )
            await session.commit()
            outbox_id = outbox.id

        async with migrated_db.session() as session:
            result = await session.execute(
                select(OutboxAttachment).where(OutboxAttachment.outbox_id == outbox_id)
            )
            attachments = list(result.scalars().all())

        assert len(attachments) == 1
        assert attachments[0].filename == "invoice.pdf"
        assert attachments[0].data == b"%PDF-1.4 fake"
