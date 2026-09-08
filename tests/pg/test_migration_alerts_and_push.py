"""
0022 against a database that already holds a calendar_prefs row in the
shape the current release produces -- an is_enabled column already made
nullable by 0021, so the new pair of columns has real neighbours rather
than an empty table. Also proves the two new unique indexes are real
database constraints, not just documentation on the ORM model, and that
the whole revision comes back down and goes up again cleanly.
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

_EXISTING_CALENDAR = uuid.UUID("55555555-5555-5555-5555-555555555551")


async def _upgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.upgrade, _alembic_config(url), revision)


async def _downgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.downgrade, _alembic_config(url), revision)


@pytest_asyncio.fixture()
async def db_at_0021(postgres_url: str) -> AsyncIterator[str]:
    """A throwaway database migrated to 0021 (the last revision before this
    one), with the PostIMAP tables 0002/0007 read stubbed. Dropped afterwards."""
    name = f"alertspush_{uuid.uuid4().hex[:12]}"
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

    await _upgrade(url, "0021_calendar_prefs_undecided")
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


async def _seed_calendar_prefs(url: str) -> None:
    """A row in the shape 0021 leaves behind: is_enabled already NULL,
    written before either new column existed."""
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO calendar_prefs (collection_id, is_visible) VALUES (:id, true)"
            ),
            {"id": _EXISTING_CALENDAR},
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_an_existing_calendar_gets_no_opinion_on_reminders(db_at_0021: str) -> None:
    """A row from before this migration must not be silently opted into (or
    out of) reminders -- it needs to read as undecided, the same as a fresh
    row, so it inherits the global default rather than something implicit."""
    await _seed_calendar_prefs(db_at_0021)

    await _upgrade(db_at_0021, "head")

    engine = create_async_engine(db_at_0021)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT default_reminder_minutes, reminders_enabled "
                    "FROM calendar_prefs WHERE collection_id = :id"
                ),
                {"id": _EXISTING_CALENDAR},
            )
        ).one()
    await engine.dispose()
    assert row.default_reminder_minutes is None
    assert row.reminders_enabled is None


@pytest.mark.asyncio
async def test_dedupe_key_is_a_real_database_constraint(db_at_0021: str) -> None:
    """Not application logic: a second insert sharing a dedupe_key must be
    refused by Postgres itself, since that refusal is the entire
    fires-exactly-once mechanism the design relies on."""
    await _upgrade(db_at_0021, "head")

    engine = create_async_engine(db_at_0021)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO alerts (kind, deliver_at, dedupe_key) "
                "VALUES ('mail', now(), 'mail:acct:msgkey')"
            )
        )
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO alerts (kind, deliver_at, dedupe_key) "
                    "VALUES ('reminder', now(), 'mail:acct:msgkey')"
                )
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_second_subscription_cannot_reuse_an_endpoint(db_at_0021: str) -> None:
    await _upgrade(db_at_0021, "head")

    engine = create_async_engine(db_at_0021)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO push_subscriptions (endpoint, p256dh, auth) "
                "VALUES ('https://push.example/1', 'key', 'auth')"
            )
        )
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO push_subscriptions (endpoint, p256dh, auth) "
                    "VALUES ('https://push.example/1', 'other-key', 'other-auth')"
                )
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_revision_comes_back_down_and_up_again(db_at_0021: str) -> None:
    """The acceptance check this block is built against: forward, then
    backward, over data the upgrade itself produced along the way."""
    await _seed_calendar_prefs(db_at_0021)
    await _upgrade(db_at_0021, "head")

    await _downgrade(db_at_0021, "0021_calendar_prefs_undecided")

    engine = create_async_engine(db_at_0021)
    async with engine.connect() as conn:
        tables = {
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' "
                        "AND table_name IN ('alerts', 'push_subscriptions')"
                    )
                )
            ).all()
        }
        columns = {
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'calendar_prefs' "
                        "AND column_name IN ('default_reminder_minutes', 'reminders_enabled')"
                    )
                )
            ).all()
        }
    await engine.dispose()
    assert tables == set()
    assert columns == set()

    # And back up again -- the same revision must be re-appliable, not a
    # one-way trip that happens to have worked once.
    await _upgrade(db_at_0021, "head")

    engine = create_async_engine(db_at_0021)
    async with engine.connect() as conn:
        value = await conn.scalar(
            text(
                "SELECT default_reminder_minutes FROM calendar_prefs "
                "WHERE collection_id = :id"
            ),
            {"id": _EXISTING_CALENDAR},
        )
    await engine.dispose()
    assert value is None
