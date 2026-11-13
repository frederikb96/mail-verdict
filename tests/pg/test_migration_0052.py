"""
0052_folder_pipeline_switch against a mirror that already holds a Gmail
account, a second server with a folder holding everything, and folders a
user had already given an override: the Gmail account's label folders are
switched off and its All Mail becomes the Archive, the other server's
folder becomes the Archive, and nothing a user set is overwritten.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from tests.pg.test_migration_from_v1 import _POSTIMAP_STUBS, _alembic_config, _upgrade

_PREVIOUS = "0051_order_files_owner"
_REVISION = "0052_folder_pipeline_switch"

_GMAIL = uuid.uuid4()
_OTHER = uuid.uuid4()
_FOLDERS = {
    name: uuid.uuid4()
    for name in (
        "gmail-inbox", "gmail-all", "gmail-starred", "gmail-label", "gmail-label-kept",
        "gmail-deleted-label", "other-all", "other-plain",
    )
}


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


_FOLDER = (
    "INSERT INTO folders (id, account_id, special_use, deleted_at) "
    "VALUES (:id, :account, :role, :deleted)"
)


@pytest_asyncio.fixture()
async def db_at_previous(postgres_url: str) -> AsyncIterator[str]:
    name = f"folderswitch0052_{uuid.uuid4().hex[:12]}"
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
    f = _FOLDERS
    await _execute(
        url,
        ("INSERT INTO accounts (id, name, imap_host) VALUES (:id, 'g', 'imap.gmail.com')",
         {"id": _GMAIL}),
        ("INSERT INTO accounts (id, name, imap_host) VALUES (:id, 'o', 'imap.example.com')",
         {"id": _OTHER}),
        (_FOLDER, {"id": f["gmail-inbox"], "account": _GMAIL, "role": "inbox", "deleted": None}),
        (_FOLDER, {"id": f["gmail-all"], "account": _GMAIL, "role": "all", "deleted": None}),
        (_FOLDER, {"id": f["gmail-starred"], "account": _GMAIL, "role": "flagged",
                   "deleted": None}),
        (_FOLDER, {"id": f["gmail-label"], "account": _GMAIL, "role": None, "deleted": None}),
        (_FOLDER, {"id": f["gmail-label-kept"], "account": _GMAIL, "role": None,
                   "deleted": None}),
        (_FOLDER, {"id": f["gmail-deleted-label"], "account": _GMAIL, "role": None,
                   "deleted": datetime(2026, 1, 1, tzinfo=timezone.utc)}),
        (_FOLDER, {"id": f["other-all"], "account": _OTHER, "role": "all", "deleted": None}),
        (_FOLDER, {"id": f["other-plain"], "account": _OTHER, "role": None, "deleted": None}),
        ("INSERT INTO folder_prefs (folder_id, display_name) VALUES (:id, 'Kept')",
         {"id": f["gmail-label-kept"]}),
    )
    try:
        yield url
    finally:
        admin = create_async_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_existing_gmail_and_all_folders_get_the_defaults(db_at_previous: str) -> None:
    await _upgrade(db_at_previous, _REVISION)

    prefs = {
        row.folder_id: (row.special_use_override, row.pipeline_enabled)
        for row in await _rows(
            db_at_previous,
            "SELECT folder_id, special_use_override, pipeline_enabled FROM folder_prefs",
        )
    }
    f = _FOLDERS
    assert prefs == {
        f["gmail-all"]: ("archive", None),
        f["gmail-starred"]: (None, False),
        f["gmail-label"]: (None, False),
        f["gmail-label-kept"]: (None, False),
        f["other-all"]: ("archive", None),
    }
    kept = await _rows(
        db_at_previous,
        f"SELECT display_name FROM folder_prefs WHERE folder_id = '{f['gmail-label-kept']}'",
    )
    assert [row.display_name for row in kept] == ["Kept"]


@pytest.mark.asyncio
async def test_downgrading_drops_the_column(db_at_previous: str) -> None:
    await _upgrade(db_at_previous, _REVISION)

    await asyncio.to_thread(command.downgrade, _alembic_config(db_at_previous), _PREVIOUS)

    columns = await _rows(
        db_at_previous,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'folder_prefs' AND column_name = 'pipeline_enabled'",
    )
    assert columns == []
