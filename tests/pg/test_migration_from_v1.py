"""
The 1.0.0 to 2.0.0 upgrade, run against a database that has data in it.

Every other test migrates an empty database straight to head, which is the
one shape where a data migration cannot fail: its backfill loop never runs
and the indexes it builds have nothing to conflict with. These build the
state a 1.0.0 deployment actually reaches -- including the duplicate
verdicts that deployment's own bug produced -- and then upgrade.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from tests.setup.migrations import _REPO_ROOT

_ACCOUNT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_MESSAGE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

# The PostIMAP-owned tables the migration chain reads: accounts and
# messages for 0002's backfill, folders for 0007's watermark backfill.
# MailVerdict's own tables carry no foreign keys onto them, so stubs with
# the columns the migrations actually read are enough, and keep this test
# independent of PostIMAP's schema version.
_POSTIMAP_STUBS = """
CREATE TABLE accounts (id uuid PRIMARY KEY, name text);
CREATE TABLE messages (
    id uuid PRIMARY KEY, account_id uuid, from_addr text, subject text,
    received_at timestamptz, size_bytes integer
);
CREATE TABLE folders (
    id uuid PRIMARY KEY, account_id uuid,
    initial_sync_done boolean NOT NULL DEFAULT false, deleted_at timestamptz
);
"""


def _alembic_config(url: str) -> Config:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    os.environ["MAIL_VERDICT_DATABASE_URL"] = url
    return cfg


async def _upgrade(url: str, revision: str) -> None:
    """Run one Alembic upgrade off the event loop -- alembic/env.py drives
    its own asyncio.run(), which cannot be called from inside a loop."""
    await asyncio.to_thread(command.upgrade, _alembic_config(url), revision)


@pytest_asyncio.fixture()
async def v1_database(postgres_url: str) -> AsyncIterator[str]:
    """A throwaway database migrated to the 1.0.0 baseline, with the two
    PostIMAP tables stubbed. Dropped afterwards."""
    name = f"v1upgrade_{uuid.uuid4().hex[:12]}"
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

    await _upgrade(url, "0001_v1_baseline")
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


async def _seed_headerless_verdicts(url: str, *, count: int) -> None:
    """One message with no Message-ID header, and `count` AI verdicts on it.

    That is what 1.0.0 produced: the durability gate was keyed on the header,
    so a message without one was reclassified on every resync, and the old
    partial index excluded NULL headers so nothing refused the extra rows.
    """
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO accounts (id, name) VALUES (:id, 'upgrade-probe')"),
            {"id": _ACCOUNT_ID},
        )
        await conn.execute(
            text(
                "INSERT INTO messages (id, account_id, from_addr, subject, "
                "received_at, size_bytes) VALUES (:id, :account_id, "
                "'sender@example.com', 'No header here', "
                "'2026-08-01T10:00:00Z', 4096)"
            ),
            {"id": _MESSAGE_ID, "account_id": _ACCOUNT_ID},
        )
        for _ in range(count):
            await conn.execute(
                text(
                    "INSERT INTO verdicts (mail_id, account_id, message_id_hdr, "
                    "is_spam, source) VALUES (:mail_id, :account_id, NULL, false, 'ai')"
                ),
                {"mail_id": _MESSAGE_ID, "account_id": _ACCOUNT_ID},
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_upgrading_with_a_single_verdict_succeeds(v1_database: str) -> None:
    """0002's backfill only runs when there is a row to back-fill, so an
    empty database proves nothing about it."""
    await _seed_headerless_verdicts(v1_database, count=1)

    await _upgrade(v1_database, "head")

    engine = create_async_engine(v1_database)
    async with engine.connect() as conn:
        msg_key = (
            await conn.execute(text("SELECT msg_key FROM verdicts"))
        ).scalar_one()
    await engine.dispose()
    assert msg_key.startswith("sha256:")


@pytest.mark.asyncio
async def test_duplicate_headerless_verdicts_collapse_instead_of_aborting(
    v1_database: str,
) -> None:
    """Duplicates are not a rare shape here -- they are what the bug 0002
    closes was producing, so a deployment that needs this migration is the
    one most likely to have them. They all derive the same msg_key from the
    same message, so the new unique index refuses to build until they are
    collapsed."""
    await _seed_headerless_verdicts(v1_database, count=3)

    await _upgrade(v1_database, "head")

    engine = create_async_engine(v1_database)
    async with engine.connect() as conn:
        remaining = (
            await conn.execute(
                text("SELECT count(*) FROM verdicts WHERE source = 'ai'")
            )
        ).scalar_one()
    await engine.dispose()
    assert remaining == 1
