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
from testcontainers.core.container import DockerContainer

from alembic import command
from mail_verdict.config.loader import reset_config
from mail_verdict.database.connection import DatabaseConnection, close_database, init_database

pytestmark = pytest.mark.pg

_REPO_ROOT = Path(__file__).parent.parent.parent


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
