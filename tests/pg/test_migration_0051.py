"""
0051_order_files_owner against files in every state a deployment holds: one
placed in an order of some user, one still waiting for its order, one placed
in an order nobody owns. A placed file takes its order's user; the others
go to the owner row.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from tests.pg.test_migration_from_v1 import _POSTIMAP_STUBS, _alembic_config, _upgrade

_PREVIOUS = "0050_users_and_ownership"
_REVISION = "0051_order_files_owner"
_OWNER = uuid.UUID("00000000-0000-0000-0000-000000000001")

_OTHER_USER = uuid.uuid4()
_ORDER_OF_OTHER = uuid.uuid4()
_ORDER_OF_NOBODY = uuid.uuid4()
_FILE_PLACED = uuid.uuid4()
_FILE_WAITING = uuid.uuid4()
_FILE_IN_UNOWNED = uuid.uuid4()


async def _execute(url: str, *statements: tuple[str, dict[str, Any]]) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            for statement, params in statements:
                await conn.execute(text(statement), params)
    finally:
        await engine.dispose()


async def _rows(url: str, query: str) -> list[Any]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return list((await conn.execute(text(query))).all())
    finally:
        await engine.dispose()


_FILE = (
    "INSERT INTO order_files (id, order_id, filename, size_bytes, sha256, data) "
    "VALUES (:id, :order, 'f.pdf', 1, :sha, '\\x00')"
)


@pytest_asyncio.fixture()
async def db_at_previous(postgres_url: str) -> AsyncIterator[str]:
    name = f"orderfiles0051_{uuid.uuid4().hex[:12]}"
    admin_url = postgres_url.rsplit("/", 1)[0]
    admin = create_async_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    await admin.dispose()

    url = f"{admin_url}/{name}"
    stubs = _POSTIMAP_STUBS + "CREATE TABLE dav_accounts (id uuid PRIMARY KEY, name text);"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        for statement in filter(None, (s.strip() for s in stubs.split(";"))):
            await conn.execute(text(statement))
    await engine.dispose()

    await _upgrade(url, _PREVIOUS)
    await _execute(
        url,
        ("INSERT INTO users (id, display_name) VALUES (:id, 'Other')", {"id": _OTHER_USER}),
        (
            "INSERT INTO orders (id, user_id) VALUES (:id, :user)",
            {"id": _ORDER_OF_OTHER, "user": _OTHER_USER},
        ),
        ("INSERT INTO orders (id) VALUES (:id)", {"id": _ORDER_OF_NOBODY}),
        (_FILE, {"id": _FILE_PLACED, "order": _ORDER_OF_OTHER, "sha": "a"}),
        (_FILE, {"id": _FILE_WAITING, "order": None, "sha": "b"}),
        (_FILE, {"id": _FILE_IN_UNOWNED, "order": _ORDER_OF_NOBODY, "sha": "c"}),
    )
    try:
        yield url
    finally:
        admin = create_async_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_a_placed_file_takes_its_orders_user_and_the_rest_go_to_the_owner(
    db_at_previous: str,
) -> None:
    await _upgrade(db_at_previous, _REVISION)

    owners = dict(await _rows(db_at_previous, "SELECT id, user_id FROM order_files"))

    assert owners == {
        _FILE_PLACED: _OTHER_USER, _FILE_WAITING: _OWNER, _FILE_IN_UNOWNED: _OWNER,
    }


@pytest.mark.asyncio
async def test_downgrading_drops_the_column(db_at_previous: str) -> None:
    await _upgrade(db_at_previous, _REVISION)

    await asyncio.to_thread(command.downgrade, _alembic_config(db_at_previous), _PREVIOUS)

    columns = await _rows(
        db_at_previous,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'order_files' AND column_name = 'user_id'",
    )
    assert columns == []
