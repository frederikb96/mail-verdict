"""
0031 against the state the previous release actually reaches: folders
carrying a unified name in folder_prefs (two sharing one, one alone, one
NULL, one empty string) and a stored sidebar order that lists a name twice
and a name no folder carries. The upgrade has to turn that into views with
the right members and positions; the downgrade has to give the old column
and order back, and the revision has to go up again cleanly.
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

_PREVIOUS = "0030_outbox_submissions"
_THIS = "0031_unified_views"


async def _upgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.upgrade, _alembic_config(url), revision)


async def _downgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.downgrade, _alembic_config(url), revision)


@pytest_asyncio.fixture()
async def db_at_previous(postgres_url: str) -> AsyncIterator[str]:
    """A throwaway database migrated to the revision before this one, with
    the PostIMAP tables earlier revisions read stubbed. Dropped afterwards."""
    name = f"unifiedviews_{uuid.uuid4().hex[:12]}"
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
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_upgrade_turns_unified_names_into_views_and_downgrade_restores_them(
    db_at_previous: str,
) -> None:
    inbox_a, inbox_b, work, archive, unnamed, empty = (uuid.uuid4() for _ in range(6))
    engine = create_async_engine(db_at_previous)
    try:
        async with engine.begin() as conn:
            for folder_id, unified_name in [
                (inbox_a, "Inbox"), (inbox_b, "Inbox"), (work, "Work"),
                (archive, "Archive"), (unnamed, None), (empty, ""),
            ]:
                await conn.execute(
                    text(
                        "INSERT INTO folder_prefs (folder_id, is_visible, unified_name) "
                        "VALUES (:folder_id, true, :name)"
                    ),
                    {"folder_id": folder_id, "name": unified_name},
                )
            await conn.execute(
                text(
                    "INSERT INTO settings (category, data) "
                    "VALUES ('unified_view', CAST(:data AS jsonb))"
                ),
                {"data": json.dumps({"folder_order": ["Work", "Missing", "Work", "Inbox"]})},
            )

        await _upgrade(db_at_previous, _THIS)

        async with engine.connect() as conn:
            views = (await conn.execute(
                text("SELECT name, position, id FROM unified_views ORDER BY position")
            )).all()
            # Listed names keep their stored order (a repeat keeps its
            # first place); an unlisted one goes after them.
            assert [(v.name, v.position) for v in views] == [
                ("Work", 0), ("Inbox", 1), ("Archive", 2),
            ]
            by_name = {v.name: v.id for v in views}
            members = (await conn.execute(
                text("SELECT view_id, folder_id FROM unified_view_folders")
            )).all()
            assert {(m.view_id, m.folder_id) for m in members} == {
                (by_name["Inbox"], inbox_a), (by_name["Inbox"], inbox_b),
                (by_name["Work"], work), (by_name["Archive"], archive),
            }
            column = await conn.scalar(text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'folder_prefs' AND column_name = 'unified_name'"
            ))
            assert column == 0
            order_rows = await conn.scalar(
                text("SELECT count(*) FROM settings WHERE category = 'unified_view'")
            )
            assert order_rows == 0

        # A second membership for a folder -- the shape the old column
        # cannot hold -- so the downgrade has to pick one.
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO unified_view_folders (view_id, folder_id) VALUES (:v, :f)"),
                {"v": by_name["Work"], "f": inbox_a},
            )

        await _downgrade(db_at_previous, _PREVIOUS)

        async with engine.connect() as conn:
            names = dict((await conn.execute(
                text("SELECT folder_id, unified_name FROM folder_prefs")
            )).all())
            assert names[inbox_a] == "Work"  # first by position
            assert names[inbox_b] == "Inbox"
            assert names[work] == "Work"
            assert names[archive] == "Archive"
            assert names[unnamed] is None
            order = await conn.scalar(
                text("SELECT data FROM settings WHERE category = 'unified_view'")
            )
            assert order == {"folder_order": ["Work", "Inbox", "Archive"]}

        await _upgrade(db_at_previous, "head")
    finally:
        await engine.dispose()
