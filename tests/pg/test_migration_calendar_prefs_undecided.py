"""
0021's backfill, run against a database that already holds the prefs rows
it exists to correct.

Migrating an empty database to head cannot exercise a data migration at
all -- the UPDATE matches nothing. This builds what a 5.0.0 deployment
actually reaches: a prefs row written to hide a to-do list in an older
interface, carrying the `is_enabled = true` the NOT NULL column defaulted
to and nobody chose, next to one that was deliberately switched off.
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

_UNTOUCHED = uuid.UUID("44444444-4444-4444-4444-444444444441")
_SWITCHED_OFF = uuid.UUID("44444444-4444-4444-4444-444444444442")


async def _upgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.upgrade, _alembic_config(url), revision)


@pytest_asyncio.fixture()
async def db_at_0020(postgres_url: str) -> AsyncIterator[str]:
    """A throwaway database migrated to 0020 (the last revision before
    0021's backfill), with the PostIMAP tables the earlier revisions read
    stubbed. Dropped afterwards."""
    name = f"prefsundecided_{uuid.uuid4().hex[:12]}"
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

    await _upgrade(url, "0020_pending_sends")
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


async def _seed_prefs(url: str) -> None:
    """Two rows in the shape 5.0.0 produces: one written for an unrelated
    reason and carrying the column's own default, one explicitly off."""
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO calendar_prefs (collection_id, is_visible, is_enabled) "
                "VALUES (:id, false, true)"
            ),
            {"id": _UNTOUCHED},
        )
        await conn.execute(
            text(
                "INSERT INTO calendar_prefs (collection_id, is_visible, is_enabled) "
                "VALUES (:id, true, false)"
            ),
            {"id": _SWITCHED_OFF},
        )
    await engine.dispose()


async def _read_is_enabled(url: str, collection_id: uuid.UUID) -> bool | None:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        value: bool | None = await conn.scalar(
            text("SELECT is_enabled FROM calendar_prefs WHERE collection_id = :id"),
            {"id": collection_id},
        )
    await engine.dispose()
    return value


@pytest.mark.asyncio
async def test_a_row_that_never_chose_becomes_undecided(db_at_0020: str) -> None:
    await _seed_prefs(db_at_0020)

    await _upgrade(db_at_0020, "head")

    assert await _read_is_enabled(db_at_0020, _UNTOUCHED) is None


@pytest.mark.asyncio
async def test_a_deliberately_disabled_calendar_stays_disabled(db_at_0020: str) -> None:
    await _seed_prefs(db_at_0020)

    await _upgrade(db_at_0020, "head")

    assert await _read_is_enabled(db_at_0020, _SWITCHED_OFF) is False


@pytest.mark.asyncio
async def test_a_row_written_afterwards_carries_no_answer_of_its_own(db_at_0020: str) -> None:
    """The half a backfill cannot cover: the next prefs row created for
    an unrelated reason must arrive undecided rather than enabled, or the
    same collections drift back on one at a time."""
    await _upgrade(db_at_0020, "head")

    engine = create_async_engine(db_at_0020)
    collection_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO calendar_prefs (collection_id, is_visible) VALUES (:id, false)"),
            {"id": collection_id},
        )
    await engine.dispose()

    assert await _read_is_enabled(db_at_0020, collection_id) is None
