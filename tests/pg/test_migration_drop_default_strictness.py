"""
0044 removes the stored semantic.default_strictness value from a settings
row that carries it, leaving every other field and every other category
untouched. Migrating an empty database to head cannot exercise this, since
the UPDATE matches nothing there.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from tests.pg.test_migration_from_v1 import _POSTIMAP_STUBS, _alembic_config

_PREVIOUS = "0043_order_controls"


async def _upgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.upgrade, _alembic_config(url), revision)


@pytest_asyncio.fixture()
async def db_at_0043(postgres_url: str) -> AsyncIterator[str]:
    """A throwaway database migrated to 0043 (the last revision before this
    migration), with the PostIMAP tables earlier revisions read stubbed.
    Dropped afterwards."""
    name = f"dropstrict_{uuid.uuid4().hex[:12]}"
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


async def _seed(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        for category, data in (
            ("semantic", {"model": "text-embedding-3-small", "default_strictness": "strict"}),
            ("retry", {"max_retries": 6}),
        ):
            await conn.execute(
                text("INSERT INTO settings (category, data) VALUES (:c, :d)"),
                {"c": category, "d": json.dumps(data)},
            )
    await engine.dispose()


async def _read_data(url: str, category: str) -> dict[str, object]:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        value: dict[str, object] | None = await conn.scalar(
            text("SELECT data FROM settings WHERE category = :category"),
            {"category": category},
        )
    await engine.dispose()
    assert value is not None
    return value


@pytest.mark.asyncio
async def test_the_stored_strictness_is_removed_and_the_rest_of_the_row_kept(
    db_at_0043: str,
) -> None:
    await _seed(db_at_0043)

    await _upgrade(db_at_0043, "head")

    assert await _read_data(db_at_0043, "semantic") == {"model": "text-embedding-3-small"}


@pytest.mark.asyncio
async def test_another_category_is_left_alone(db_at_0043: str) -> None:
    await _seed(db_at_0043)

    await _upgrade(db_at_0043, "head")

    assert await _read_data(db_at_0043, "retry") == {"max_retries": 6}
