"""
pg-layer fixtures: a migrated database next to a real PostIMAP, zero
accounts. Every test collected under tests/pg/ is auto-marked `pg`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer

from alembic import command
from mail_verdict.config.loader import reset_config
from mail_verdict.database.connection import DatabaseConnection, close_database, init_database
from tests.setup.containers import POSTGRES_DB

pytestmark = pytest.mark.pg

_REPO_ROOT = Path(__file__).parent.parent.parent

# A LOGIN role carrying nothing but GRANT postimap_app -- the actual write
# boundary a production deployment runs under (consumer-contract.md:
# "GRANT postimap_app TO your_app_role"). Every other pg fixture connects
# as the Postgres owner, so a write to a column outside the contract would
# pass silently there; this is the one connection that would catch it.
_RESTRICTED_ROLE = "mailverdict_test_app"
_RESTRICTED_PASSWORD = "restricted-test-role"  # noqa: S105 -- throwaway, container-local only


async def _run_migrations(database_url: str) -> None:
    """Run Alembic upgrade to head against the given database URL.

    alembic/env.py drives its own asyncio.run() internally; called directly
    from an async fixture that is itself already inside pytest-asyncio's
    event loop, that raises "asyncio.run() cannot be called from a running
    event loop". Off-loading the whole synchronous command.upgrade() call
    to a worker thread gives alembic a thread with no running loop to
    create its own in.
    """
    os.environ["MAIL_VERDICT_DATABASE_URL"] = database_url
    reset_config()

    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    await asyncio.to_thread(command.upgrade, cfg, "head")


@pytest_asyncio.fixture()
async def migrated_db(
    postgres_url: str, postimap_container: DockerContainer,
) -> AsyncIterator[DatabaseConnection]:
    """
    A DatabaseConnection to a Postgres migrated with both PostIMAP's own
    schema (via the postimap_container fixture, which only becomes ready
    after PostIMAP finishes its own migrations) and MailVerdict's owned
    tables (via Alembic here) -- proving the two can migrate independently,
    in either order, with no FK coupling between them.
    """
    await _run_migrations(postgres_url)

    db = await init_database_for_url(postgres_url)
    try:
        yield db
    finally:
        await close_database()
        reset_config()


async def init_database_for_url(database_url: str) -> DatabaseConnection:
    """Initialize the global DatabaseConnection against a specific URL."""
    from mail_verdict.config.loader import DatabaseConfig

    return await init_database(
        DatabaseConfig(url=database_url, pool_size=5, max_overflow=0)
    )


@pytest_asyncio.fixture()
async def restricted_db(
    migrated_db: DatabaseConnection, postgres_container: PostgresContainer,
) -> AsyncIterator[DatabaseConnection]:
    """
    A DatabaseConnection authenticated as a LOGIN role granted only
    postimap_app -- not the Postgres owner every other pg fixture connects
    as. A standalone instance, deliberately not routed through the global
    init_database() singleton migrated_db already occupies.
    """
    async with migrated_db.session() as session:
        try:
            await session.execute(
                text(
                    f"CREATE ROLE {_RESTRICTED_ROLE} LOGIN PASSWORD "
                    f"'{_RESTRICTED_PASSWORD}'"
                )
            )
            await session.commit()
        except ProgrammingError:
            # Role already exists from an earlier test in this container
            # session -- role creation is cluster-wide, not per-test.
            await session.rollback()

        await session.execute(text(f"GRANT postimap_app TO {_RESTRICTED_ROLE}"))
        await session.commit()

    from mail_verdict.config.loader import DatabaseConfig

    host = postgres_container.get_container_host_ip()
    port = postgres_container.get_exposed_port(5432)
    restricted_url = (
        f"postgresql+asyncpg://{_RESTRICTED_ROLE}:{_RESTRICTED_PASSWORD}"
        f"@{host}:{port}/{POSTGRES_DB}"
    )

    restricted = DatabaseConnection(DatabaseConfig(url=restricted_url, pool_size=2, max_overflow=0))
    await restricted.init()
    try:
        yield restricted
    finally:
        await restricted.close()
