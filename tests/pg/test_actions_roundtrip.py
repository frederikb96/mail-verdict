"""
postimap/actions.py SQL round-trips against a real PostIMAP-migrated schema.

Account/folder/message rows are seeded with raw SQL rather than through a
real IMAP sync -- these tests are about actions.py's SQL shape (does the
UPDATE hit the columns the contract says it may, does the format byte land
correctly), not about PostIMAP's own sync engine.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Account, Message, Verdict, VerdictSource
from mail_verdict.postimap.actions import (
    create_account,
    expunge,
    format_credential,
    move_message,
    set_flags,
)


async def _seed_account_folder_message(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert a minimal account/folder/message chain via raw SQL, return their ids."""
    account_id = uuid.uuid4()
    inbox_id = uuid.uuid4()
    junk_id = uuid.uuid4()
    message_id = uuid.uuid4()

    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    for folder_id, imap_name, special_use in (
        (inbox_id, "INBOX", None),
        (junk_id, "Junk", "junk"),
    ):
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
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id) "
            "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :message_id)"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": inbox_id,
            "thread_id": uuid.uuid4(), "message_id": "<seed-1@example.com>",
        },
    )
    return account_id, inbox_id, message_id


class TestFormatCredential:
    """Tests for the contract's consumer credential format."""

    def test_writes_plaintext_format_byte(self) -> None:
        """The written bytes start with the 0x00 plaintext format prefix."""
        result = format_credential("hunter2")
        assert result[0:1] == b"\x00"
        assert result[1:] == b"hunter2"


class TestCreateAccount:
    """Tests for postimap/actions.create_account against a real accounts table."""

    @pytest.mark.asyncio
    async def test_password_lands_with_format_prefix(self, migrated_db: DatabaseConnection) -> None:
        """The account row's imap_password is 0x00-prefixed, not bare UTF-8."""
        async with migrated_db.session() as session:
            account = await create_account(
                session,
                name=f"test-{uuid.uuid4()}",
                imap_host="imap.example.com",
                imap_port=993,
                imap_user="user@example.com",
                imap_password="hunter2",
            )
            await session.commit()

        async with migrated_db.session() as session:
            result = await session.execute(select(Account).where(Account.id == account.id))
            row = result.scalar_one()

        assert row.imap_password[0:1] == b"\x00"
        assert row.imap_password[1:] == b"hunter2"


class TestMoveMessage:
    """Tests for the optimistic move: folder_id + imap_uid=NULL together."""

    @pytest.mark.asyncio
    async def test_move_sets_target_folder_and_nulls_uid(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """After a move, folder_id is the target and imap_uid is NULL (pending)."""
        async with migrated_db.session() as session:
            account_id, _inbox_id, message_id = await _seed_account_folder_message(session)
            result = await session.execute(
                text("SELECT id FROM folders WHERE special_use = 'junk' AND account_id = :aid"),
                {"aid": account_id},
            )
            junk_id = result.scalar_one()

            await move_message(session, message_id, junk_id)
            await session.commit()

        async with migrated_db.session() as session:
            result = await session.execute(select(Message).where(Message.id == message_id))
            row = result.scalar_one()

        assert row.folder_id == junk_id
        assert row.imap_uid is None


class TestExpunge:
    """Tests for the hard-delete path: expunged_at set, row survives."""

    @pytest.mark.asyncio
    async def test_expunge_sets_timestamp_without_deleting_row(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """expunged_at is set; the row itself is not removed."""
        async with migrated_db.session() as session:
            _account_id, _inbox_id, message_id = await _seed_account_folder_message(session)
            await expunge(session, message_id)
            await session.commit()

        async with migrated_db.session() as session:
            result = await session.execute(select(Message).where(Message.id == message_id))
            row = result.scalar_one()

        assert row.expunged_at is not None


class TestSetFlags:
    """Tests for flag updates via set_flags."""

    @pytest.mark.asyncio
    async def test_sets_multiple_flags_at_once(self, migrated_db: DatabaseConnection) -> None:
        """Multiple flags passed together are all applied in one UPDATE."""
        async with migrated_db.session() as session:
            _account_id, _inbox_id, message_id = await _seed_account_folder_message(session)
            await set_flags(session, message_id, is_seen=True, is_flagged=True)
            await session.commit()

        async with migrated_db.session() as session:
            result = await session.execute(select(Message).where(Message.id == message_id))
            row = result.scalar_one()

        assert row.is_seen is True
        assert row.is_flagged is True


class TestVerdictDurabilityGate:
    """Tests for the partial unique index backing the never-reclassify gate."""

    @pytest.mark.asyncio
    async def test_second_ai_verdict_for_same_header_is_rejected(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """A second source=ai verdict for the same (account_id, message_id_hdr) violates
        the partial unique index -- the durability gate is enforced by the schema, not
        merely by application code remembering to check first."""
        account_id = uuid.uuid4()
        header = f"<{uuid.uuid4()}@example.com>"

        async with migrated_db.session() as session:
            session.add(
                Verdict(
                    mail_id=uuid.uuid4(), account_id=account_id,
                    message_id_hdr=header, is_spam=False, source=VerdictSource.AI,
                )
            )

        with pytest.raises(IntegrityError):
            async with migrated_db.session() as session:
                session.add(
                    Verdict(
                        mail_id=uuid.uuid4(), account_id=account_id,
                        message_id_hdr=header, is_spam=True, source=VerdictSource.AI,
                    )
                )

    @pytest.mark.asyncio
    async def test_user_feedback_verdict_for_same_header_is_allowed(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The unique index only constrains source=ai -- feedback can still be logged."""
        account_id = uuid.uuid4()
        header = f"<{uuid.uuid4()}@example.com>"

        async with migrated_db.session() as session:
            session.add(
                Verdict(
                    mail_id=uuid.uuid4(), account_id=account_id,
                    message_id_hdr=header, is_spam=False, source=VerdictSource.AI,
                )
            )

        async with migrated_db.session() as session:
            session.add(
                Verdict(
                    mail_id=uuid.uuid4(), account_id=account_id,
                    message_id_hdr=header, is_spam=True, source=VerdictSource.USER_FEEDBACK,
                )
            )
