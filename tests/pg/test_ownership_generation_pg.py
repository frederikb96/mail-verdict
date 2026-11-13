"""
A change of account ownership is announced once its transaction commits --
what makes an open live-update stream look at its user's accounts again
(api/events.py) -- and never before, nor for a transaction rolled back.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

import mail_verdict.auth.ownership as ownership
from mail_verdict.auth.ownership import ownership_generation, set_account_owner
from mail_verdict.auth.principal import OWNER_USER_ID
from mail_verdict.database.connection import DatabaseConnection


async def _account(db: DatabaseConnection) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password, "
                "is_active) VALUES (:id, :name, 'imap.example.com', 993, 'u@example.com', "
                "'\\x00' || convert_to('pw', 'UTF8'), false)"
            ),
            {"id": account_id, "name": f"acct-{account_id}"},
        )
    return account_id


@pytest.mark.asyncio
async def test_a_committed_change_is_announced_once_after_the_commit(
    migrated_db: DatabaseConnection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = await _account(migrated_db)
    calls: list[int] = []
    monkeypatch.setattr(ownership, "_listeners", [lambda: calls.append(ownership_generation())])
    before = ownership_generation()

    async with migrated_db.session() as session:
        await set_account_owner(session, account_id, OWNER_USER_ID)
        await set_account_owner(session, account_id, OWNER_USER_ID)
        assert ownership_generation() == before

    assert ownership_generation() == before + 1
    assert calls == [before + 1]


@pytest.mark.asyncio
async def test_a_rolled_back_change_is_not_announced(migrated_db: DatabaseConnection) -> None:
    account_id = await _account(migrated_db)
    before = ownership_generation()

    with pytest.raises(RuntimeError):
        async with migrated_db.session() as session:
            await set_account_owner(session, account_id, OWNER_USER_ID)
            raise RuntimeError("abandoned")

    assert ownership_generation() == before
