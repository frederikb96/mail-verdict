"""
Alembic migration environment configuration.

Only manages MailVerdict-owned tables. PostIMAP-owned tables
(accounts, folders, messages, attachments, sync_queue, sync_state, sync_audit)
are created by PostIMAP's Kysely migrations and excluded here.
"""

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mail_verdict.config import get_config  # noqa: E402
from mail_verdict.database.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

POSTIMAP_TABLES = frozenset({
    "accounts", "folders", "messages", "attachments",
    "sync_queue", "sync_state", "sync_audit",
})


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object,
) -> bool:
    """Exclude PostIMAP-owned tables from Alembic autogeneration."""
    if type_ == "table" and name in POSTIMAP_TABLES:
        return False
    return True


def get_database_url() -> str:
    """Get database URL from MailVerdict config."""
    mv_config = get_config()
    return mv_config.database.url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations within a sync connection context."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    db_url = get_database_url()

    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = db_url

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
