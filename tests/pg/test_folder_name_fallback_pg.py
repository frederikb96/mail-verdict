"""
FolderRepository.resolve_special_folder()'s name-fallback path -- row 114:
the fallback only ever compared the full imap_name against a bare
candidate name, so a namespaced mailbox (INBOX.Archive, INBOX/Archive)
never matched, and picking between two matching folders (Junk and Spam
both present) was whatever Postgres happened to return with no ORDER BY.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.repository import FolderRepository


async def _seed_account(session: AsyncSession) -> uuid.UUID:
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


async def _seed_folder(session: AsyncSession, account_id: uuid.UUID, imap_name: str) -> uuid.UUID:
    folder_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO folders (id, account_id, imap_name) VALUES (:id, :account_id, :name)"),
        {"id": folder_id, "account_id": account_id, "name": imap_name},
    )
    return folder_id


class TestNamespacedFallback:
    @pytest.mark.asyncio
    async def test_dot_namespaced_archive_is_matched(self, migrated_db: DatabaseConnection) -> None:
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            archive_id = await _seed_folder(session, account_id, "INBOX.Archive")
            await session.commit()

        found = await FolderRepository(migrated_db).resolve_special_folder(account_id, "archive")
        assert found == archive_id

    @pytest.mark.asyncio
    async def test_slash_namespaced_archive_is_matched(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            archive_id = await _seed_folder(session, account_id, "INBOX/Archive")
            await session.commit()

        found = await FolderRepository(migrated_db).resolve_special_folder(account_id, "archive")
        assert found == archive_id

    @pytest.mark.asyncio
    async def test_root_level_name_still_matches(self, migrated_db: DatabaseConnection) -> None:
        """The un-namespaced case this fallback already handled must keep
        working -- the last-segment match degrades to a full match when
        there is no delimiter at all."""
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            archive_id = await _seed_folder(session, account_id, "Archive")
            await session.commit()

        found = await FolderRepository(migrated_db).resolve_special_folder(account_id, "archive")
        assert found == archive_id


class TestDeterministicChoice:
    @pytest.mark.asyncio
    async def test_junk_wins_over_spam_when_both_present(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """_ROLE_NAME_FALLBACKS lists 'junk' before 'spam' for the junk
        role -- with both folders present the choice must follow that
        order every time, not whatever the planner returns."""
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            # Insert Spam first so an id/insertion-order tiebreak alone
            # (without honouring the names tuple's own preference) would
            # pick the wrong one.
            await _seed_folder(session, account_id, "Spam")
            junk_id = await _seed_folder(session, account_id, "Junk")
            await session.commit()

        found = await FolderRepository(migrated_db).resolve_special_folder(account_id, "junk")
        assert found == junk_id

    @pytest.mark.asyncio
    async def test_result_is_stable_across_repeated_calls(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            await _seed_folder(session, account_id, "Bulk Mail")
            junk_id = await _seed_folder(session, account_id, "Junk E-Mail")
            await session.commit()

        repo = FolderRepository(migrated_db)
        results = {await repo.resolve_special_folder(account_id, "junk") for _ in range(5)}
        assert results == {junk_id}
