"""
0023 against a database already migrated to 0022 -- proves the singleton
check constraint is a real database constraint (a second row is refused,
not merely undocumented), and that the revision comes back down and up
again cleanly.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from tests.pg.test_migration_from_v1 import _POSTIMAP_STUBS, _alembic_config


async def _upgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.upgrade, _alembic_config(url), revision)


async def _downgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.downgrade, _alembic_config(url), revision)


@pytest_asyncio.fixture()
async def db_at_0022(postgres_url: str) -> AsyncIterator[str]:
    """A throwaway database migrated to 0022 (the last revision before this
    one), with the PostIMAP tables 0002/0007 read stubbed. Dropped afterwards."""
    name = f"vapidkeypair_{uuid.uuid4().hex[:12]}"
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

    await _upgrade(url, "0022_alerts_and_push")
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


@pytest.mark.asyncio
async def test_a_second_row_is_refused_by_the_database(db_at_0022: str) -> None:
    """Not application logic: the singleton is a check constraint, so a
    second row with a different id must be refused by Postgres itself."""
    await _upgrade(db_at_0022, "head")

    engine = create_async_engine(db_at_0022)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO vapid_keypair (id, encrypted_private_key) VALUES (1, :k)"),
            {"k": b"placeholder"},
        )
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO vapid_keypair (id, encrypted_private_key) VALUES (2, :k)"),
                {"k": b"placeholder"},
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_revision_comes_back_down_and_up_again(db_at_0022: str) -> None:
    await _upgrade(db_at_0022, "head")

    engine = create_async_engine(db_at_0022)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO vapid_keypair (id, encrypted_private_key) VALUES (1, :k)"),
            {"k": b"placeholder"},
        )
    await engine.dispose()

    await _downgrade(db_at_0022, "0022_alerts_and_push")

    engine = create_async_engine(db_at_0022)
    async with engine.connect() as conn:
        exists = await conn.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'vapid_keypair')"
            )
        )
    await engine.dispose()
    assert exists is False

    # And back up again -- the same revision must be re-appliable.
    await _upgrade(db_at_0022, "head")

    engine = create_async_engine(db_at_0022)
    async with engine.connect() as conn:
        count = await conn.scalar(text("SELECT count(*) FROM vapid_keypair"))
    await engine.dispose()
    assert count == 0
