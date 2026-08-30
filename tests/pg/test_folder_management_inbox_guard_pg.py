"""
DELETE /folders/{folder_id}'s INBOX guard, against a real database.

The pinned test PostIMAP image is well past the folder-CRUD grant's
version threshold (see test_actions_roundtrip.py's TestSupportsFolderCrud),
so no version needs seeding here to reach the guard itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.folder_management import folder_prefs_router
from mail_verdict.database.connection import DatabaseConnection


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(folder_prefs_router)
    with TestClient(app) as c:
        yield c


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


async def _seed_folder(
    session: AsyncSession, account_id: uuid.UUID, imap_name: str, special_use: str | None,
) -> uuid.UUID:
    folder_id = uuid.uuid4()
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
    return folder_id


async def _seed_special_use_override(
    session: AsyncSession, folder_id: uuid.UUID, role: str,
) -> None:
    await session.execute(
        text(
            "INSERT INTO folder_prefs (folder_id, special_use_override) VALUES (:folder_id, :role)"
        ),
        {"folder_id": folder_id, "role": role},
    )


async def _seed_inbox_by_raw_special_use(
    migrated_db: DatabaseConnection,
) -> uuid.UUID:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        folder_id = await _seed_folder(session, account_id, "INBOX", "inbox")
        await session.commit()
    return folder_id


async def _seed_inbox_by_imap_name_only(migrated_db: DatabaseConnection) -> uuid.UUID:
    """A server that never advertised SPECIAL-USE at all -- special_use
    is NULL, and the only thing marking this folder as INBOX is its
    case-insensitive IMAP-mandated name."""
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        folder_id = await _seed_folder(session, account_id, "inbox", None)  # lowercase, on purpose
        await session.commit()
    return folder_id


async def _seed_inbox_by_override_only(migrated_db: DatabaseConnection) -> uuid.UUID:
    """A folder named something else entirely, marked INBOX only through
    folder_prefs.special_use_override."""
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        folder_id = await _seed_folder(session, account_id, "Primary", None)
        await _seed_special_use_override(session, folder_id, "inbox")
        await session.commit()
    return folder_id


async def _seed_ordinary_folder(migrated_db: DatabaseConnection) -> uuid.UUID:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        folder_id = await _seed_folder(session, account_id, "Archive", "archive")
        await session.commit()
    return folder_id


class TestInboxGuard:
    def test_rejects_by_raw_special_use(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        folder_id = client.portal.call(_seed_inbox_by_raw_special_use, migrated_db)
        target = "mail_verdict.api.folder_management.get_db_connection"
        with patch(target, return_value=migrated_db):
            resp = client.delete(f"/folders/{folder_id}")
        assert resp.status_code == 400

    def test_rejects_by_case_insensitive_imap_name_alone(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """No SPECIAL-USE anywhere -- only the RFC-mandated name says this
        is INBOX. Before the fix, the guard tested special_use alone and
        never fired here, so deleting this folder destroyed the mailbox."""
        folder_id = client.portal.call(_seed_inbox_by_imap_name_only, migrated_db)
        target = "mail_verdict.api.folder_management.get_db_connection"
        with patch(target, return_value=migrated_db):
            resp = client.delete(f"/folders/{folder_id}")
        assert resp.status_code == 400

    def test_rejects_by_special_use_override_alone(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """A user has told MailVerdict "this folder is my inbox" on a
        server that never advertised it -- the same protection as a
        server that did."""
        folder_id = client.portal.call(_seed_inbox_by_override_only, migrated_db)
        target = "mail_verdict.api.folder_management.get_db_connection"
        with patch(target, return_value=migrated_db):
            resp = client.delete(f"/folders/{folder_id}")
        assert resp.status_code == 400

    def test_ordinary_folder_is_not_blocked_by_the_guard(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """Control case: an Archive folder is not INBOX by any measure and
        must reach the real delete, not be rejected by this guard --
        confirm_message_count=0 clears the separate delete-confirmation
        requirement, since this folder was seeded with no messages."""
        folder_id = client.portal.call(_seed_ordinary_folder, migrated_db)
        target = "mail_verdict.api.folder_management.get_db_connection"
        with patch(target, return_value=migrated_db):
            resp = client.delete(f"/folders/{folder_id}?confirm_message_count=0")
        assert resp.status_code != 400
        assert resp.status_code == 204
