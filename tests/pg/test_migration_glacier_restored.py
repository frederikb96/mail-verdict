"""
0035's data healing, run against a database that already holds the
shape a pre-fix confirm_restores left behind: restored_at set, state
still 'restoring' -- the never-reaches-a-terminal-state bug this
migration's own code fix (glacier/restore.py) closes going forward, and
this migration heals for anything already glaciered under the old code.

Every other glacier migration test migrates an empty database straight
to head, which is the one shape a data migration cannot fail in -- the
UPDATE this migration runs has nothing to match. This builds the exact
state the bug produced and upgrades through it, the same shape
test_migration_heal_stranded_intake.py uses.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from tests.pg.test_migration_from_v1 import _POSTIMAP_STUBS, _alembic_config

_ACCOUNT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


async def _upgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.upgrade, _alembic_config(url), revision)


@pytest_asyncio.fixture()
async def db_at_0034(postgres_url: str) -> AsyncIterator[str]:
    """A throwaway database migrated to 0034 (the glacier migration, the
    last revision before 0035's healing). Dropped afterwards."""
    name = f"glacierrestored_{uuid.uuid4().hex[:12]}"
    admin_url = postgres_url.rsplit("/", 1)[0]
    admin = create_async_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    await admin.dispose()

    url = f"{admin_url}/{name}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        for statement in filter(None, (s.strip() for s in _POSTIMAP_STUBS.split(";"))):
            await conn.execute(text(statement))
    await engine.dispose()

    await _upgrade(url, "0034_glacier")
    try:
        yield url
    finally:
        admin = create_async_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n"
                ),
                {"n": name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        await admin.dispose()


async def _seed_account(url: str, *, account_id: uuid.UUID) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO accounts (id, name) VALUES (:id, 'restore-heal-probe')"),
            {"id": account_id},
        )
    await engine.dispose()


async def _seed_glacier_row(
    url: str, *, account_id: uuid.UUID, glacier_id: uuid.UUID, state: str,
    restored_at_set: bool,
) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO glacier_messages "
                "(id, account_id, folder_id, thread_id, msg_key, state, restored_at) "
                "VALUES (:id, :account_id, :folder_id, :thread_id, :msg_key, :state, "
                " CASE WHEN :restored_at_set THEN now() ELSE NULL END)"
            ),
            {
                "id": glacier_id, "account_id": account_id, "folder_id": uuid.uuid4(),
                "thread_id": glacier_id, "msg_key": f"msg-{glacier_id}", "state": state,
                "restored_at_set": restored_at_set,
            },
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_pre_fix_completed_restore_is_healed_to_the_terminal_state(
    db_at_0034: str,
) -> None:
    """The exact shape the bug produced: restored_at set (confirm_restores
    ran), state left at 'restoring' (never advanced) -- the row that,
    under the old code, would re-confirm every sweep tick and could be
    flipped to failed by the restore timeout despite having completed
    minutes earlier."""
    glacier_id = uuid.uuid4()
    await _seed_account(db_at_0034, account_id=_ACCOUNT_ID)
    await _seed_glacier_row(
        db_at_0034, account_id=_ACCOUNT_ID, glacier_id=glacier_id, state="restoring",
        restored_at_set=True,
    )

    await _upgrade(db_at_0034, "head")

    engine = create_async_engine(db_at_0034)
    async with engine.connect() as conn:
        state, restored_at = (
            await conn.execute(
                text("SELECT state, restored_at FROM glacier_messages WHERE id = :id"),
                {"id": glacier_id},
            )
        ).one()
    await engine.dispose()
    assert state == "restored"
    assert restored_at is not None


@pytest.mark.asyncio
async def test_a_genuinely_in_progress_restore_is_left_alone(db_at_0034: str) -> None:
    """The predicate is state='restoring' AND restored_at IS NOT NULL -- a
    row still genuinely in flight (restored_at NULL) must survive
    untouched, not be healed into a state it never actually reached."""
    glacier_id = uuid.uuid4()
    await _seed_account(db_at_0034, account_id=_ACCOUNT_ID)
    await _seed_glacier_row(
        db_at_0034, account_id=_ACCOUNT_ID, glacier_id=glacier_id, state="restoring",
        restored_at_set=False,
    )

    await _upgrade(db_at_0034, "head")

    engine = create_async_engine(db_at_0034)
    async with engine.connect() as conn:
        state, restored_at = (
            await conn.execute(
                text("SELECT state, restored_at FROM glacier_messages WHERE id = :id"),
                {"id": glacier_id},
            )
        ).one()
    await engine.dispose()
    assert state == "restoring"
    assert restored_at is None
