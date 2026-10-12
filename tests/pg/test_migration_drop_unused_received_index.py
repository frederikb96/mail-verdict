"""
0046 drops the unused (account_id, received_at) btree on message_embeddings and
nothing else; downgrading brings it back.
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

_PREVIOUS = "0045_halfvec_embeddings"


async def _upgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.upgrade, _alembic_config(url), revision)


@pytest_asyncio.fixture()
async def db_at_0045(postgres_url: str) -> AsyncIterator[str]:
    """A throwaway database migrated to 0045 (the last revision before this
    migration), with the PostIMAP tables earlier revisions read stubbed.
    Dropped afterwards."""
    name = f"dropidx_{uuid.uuid4().hex[:12]}"
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

    await _upgrade(url, _PREVIOUS)
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


async def _downgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.downgrade, _alembic_config(url), revision)


async def _index_names(url: str) -> set[str]:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE tablename IN "
                "('message_embeddings', 'glacier_messages')"
            )
        )
        names = {row[0] for row in rows}
    await engine.dispose()
    return names


@pytest.mark.asyncio
async def test_only_the_unused_index_is_dropped_and_downgrade_restores_it(
    db_at_0045: str,
) -> None:
    before = await _index_names(db_at_0045)
    assert "ix_message_embeddings_account_received" in before

    await _upgrade(db_at_0045, "head")
    after = await _index_names(db_at_0045)
    assert after == before - {"ix_message_embeddings_account_received"}

    await _downgrade(db_at_0045, _PREVIOUS)
    assert await _index_names(db_at_0045) == before
