"""
0040's cleanup, run against the state a released deployment actually
reaches: a settings row already carrying a provider-key-shaped field the
API merged into its JSONB blob before this masking was made universal
(see the "Every migration test runs against an empty database" note in
.claude/CLAUDE.md -- migrating an empty database to head cannot exercise
this at all, since the UPDATE matches nothing).

Two shapes, both seen in practice: a field still holding a live-looking
value, and one already emptied to "" by an operator working around the
API offering no dedicated way to unset a field. Both must be removed
entirely -- an empty value left in place is still a stored field the API
strips on the way out today, but the row itself stays dirty forever under
the settings repository's own merge-on-write semantics.
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

_PREVIOUS = "0039_heal_pipeline_document"


async def _upgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.upgrade, _alembic_config(url), revision)


@pytest_asyncio.fixture()
async def db_at_0039(postgres_url: str) -> AsyncIterator[str]:
    """A throwaway database migrated to 0039 (the last revision before the
    scrub), with the PostIMAP tables earlier revisions read stubbed.
    Dropped afterwards."""
    name = f"scrubkeys_{uuid.uuid4().hex[:12]}"
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


async def _seed_settings(url: str) -> None:
    """The shape a released deployment reaches: a live-looking key leaked
    into a category that has nothing to do with "ai", an operator's own
    empty-string scrub of a different one, and an ordinary category with
    nothing credential-shaped in it at all -- the negative control."""
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO settings (category, data) VALUES ('semantic', :data)"),
            {
                "data": json.dumps({
                    "provider": "custom",
                    "base_url": "https://example.test/v1",
                    "model": "text-embedding-3-small",
                    "custom_api_key": "",
                }),
            },
        )
        await conn.execute(
            text("INSERT INTO settings (category, data) VALUES ('outbox', :data)"),
            {
                "data": json.dumps({
                    "undo_send_seconds": 5.0,
                    "openai_api_key": "sk-should-never-have-landed-here",
                }),
            },
        )
        await conn.execute(
            text("INSERT INTO settings (category, data) VALUES ('retry', :data)"),
            {"data": json.dumps({"max_retries": 6})},
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
async def test_an_emptied_key_field_is_removed_entirely(db_at_0039: str) -> None:
    """Not just cleared to "" -- gone. A field an operator scrubbed
    through the write-only API by setting it to "" (the only way it could
    be cleared before this fix) still leaves a stored field the settings
    repository's own merge-on-write semantics never drop on their own."""
    await _seed_settings(db_at_0039)

    await _upgrade(db_at_0039, "head")

    data = await _read_data(db_at_0039, "semantic")
    assert "custom_api_key" not in data


@pytest.mark.asyncio
async def test_a_live_looking_key_leaked_into_an_unrelated_category_is_removed(
    db_at_0039: str,
) -> None:
    await _seed_settings(db_at_0039)
    data_before = await _read_data(db_at_0039, "outbox")
    assert data_before["openai_api_key"] == "sk-should-never-have-landed-here"

    await _upgrade(db_at_0039, "head")

    data = await _read_data(db_at_0039, "outbox")
    assert "openai_api_key" not in data


@pytest.mark.asyncio
async def test_every_other_field_in_a_scrubbed_row_survives_untouched(db_at_0039: str) -> None:
    await _seed_settings(db_at_0039)

    await _upgrade(db_at_0039, "head")

    semantic = await _read_data(db_at_0039, "semantic")
    assert semantic["provider"] == "custom"
    assert semantic["base_url"] == "https://example.test/v1"
    assert semantic["model"] == "text-embedding-3-small"

    outbox = await _read_data(db_at_0039, "outbox")
    assert outbox["undo_send_seconds"] == 5.0


@pytest.mark.asyncio
async def test_a_row_with_nothing_credential_shaped_is_left_alone(db_at_0039: str) -> None:
    """The negative control: a category that never carried a key-shaped
    field at all must come through byte-for-byte, not merely "still
    correct" -- the migration is scoped by EXISTS precisely so a row like
    this is never even written."""
    await _seed_settings(db_at_0039)

    await _upgrade(db_at_0039, "head")

    assert await _read_data(db_at_0039, "retry") == {"max_retries": 6}
