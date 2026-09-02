"""
0017's data healing, run against a database that already holds the
stranded rows the pre-0014 bug produced.

Every other calendar_intake test migrates an empty database straight to
head, which is the one shape a data migration cannot fail in -- the
UPDATE this migration runs has nothing to match. This builds the state
a pre-0014 deployment actually reaches (a 'status=imported, object_id
NULL' strand, per row 118) and upgrades through it, the same shape
test_migration_from_v1.py uses for the 1.0.0 upgrade.
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

_ACCOUNT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


async def _upgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.upgrade, _alembic_config(url), revision)


@pytest_asyncio.fixture()
async def db_at_0016(postgres_url: str) -> AsyncIterator[str]:
    """A throwaway database migrated to 0016 (the last revision before
    0017's healing), with the two PostIMAP tables 0002/0007 read stubbed.
    Dropped afterwards."""
    name = f"healstrand_{uuid.uuid4().hex[:12]}"
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

    await _upgrade(url, "0016_intake_pending_review")
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


async def _seed_stranded_row(url: str, *, account_id: uuid.UUID) -> None:
    """The exact shape pre-0014 code produced: status='imported' written
    before _apply() ever created the object it describes, with nothing
    ever landing in object_id."""
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO accounts (id, name) VALUES (:id, 'heal-probe')"),
            {"id": account_id},
        )
        await conn.execute(
            text(
                "INSERT INTO calendar_intake "
                "(account_id, msg_key, ical_uid, method, sequence, status) "
                "VALUES (:account_id, '<stranded@example.com>', 'stranded-uid', "
                "'REQUEST', 0, 'imported')"
            ),
            {"account_id": account_id},
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_stranded_imported_row_is_rewritten_to_pending(db_at_0016: str) -> None:
    await _seed_stranded_row(db_at_0016, account_id=_ACCOUNT_ID)

    await _upgrade(db_at_0016, "head")

    engine = create_async_engine(db_at_0016)
    async with engine.connect() as conn:
        status, object_id = (
            await conn.execute(
                text("SELECT status, object_id FROM calendar_intake WHERE account_id = :a"),
                {"a": _ACCOUNT_ID},
            )
        ).one()
    await engine.dispose()
    assert status == "pending"
    assert object_id is None


@pytest.mark.asyncio
async def test_a_genuinely_completed_import_is_left_alone(db_at_0016: str) -> None:
    """The predicate is status='imported' AND object_id IS NULL -- a row
    that actually completed (object_id set) must survive untouched."""
    engine = create_async_engine(db_at_0016)
    object_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO accounts (id, name) VALUES (:id, 'heal-probe-2')"),
            {"id": _ACCOUNT_ID},
        )
        await conn.execute(
            text(
                "INSERT INTO calendar_intake "
                "(account_id, msg_key, ical_uid, method, sequence, status, object_id) "
                "VALUES (:account_id, '<completed@example.com>', 'completed-uid', "
                "'REQUEST', 0, 'imported', :object_id)"
            ),
            {"account_id": _ACCOUNT_ID, "object_id": object_id},
        )
    await engine.dispose()

    await _upgrade(db_at_0016, "head")

    engine = create_async_engine(db_at_0016)
    async with engine.connect() as conn:
        status, stored_object_id = (
            await conn.execute(
                text("SELECT status, object_id FROM calendar_intake WHERE account_id = :a"),
                {"a": _ACCOUNT_ID},
            )
        ).one()
    await engine.dispose()
    assert status == "imported"
    assert stored_object_id == object_id
