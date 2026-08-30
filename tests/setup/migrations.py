"""
Shared Alembic migration runner for the pg and e2e test layers.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from alembic.config import Config

from alembic import command
from mail_verdict.config.loader import reset_config

_REPO_ROOT = Path(__file__).parent.parent.parent


async def run_migrations(database_url: str) -> None:
    """Run Alembic upgrade to head against the given database URL.

    alembic/env.py drives its own asyncio.run() internally; called directly
    from an async caller that is itself already inside an event loop, that
    raises "asyncio.run() cannot be called from a running event loop".
    Off-loading the whole synchronous command.upgrade() call to a worker
    thread gives alembic a thread with no running loop to create its own in.
    """
    os.environ["MAIL_VERDICT_DATABASE_URL"] = database_url
    reset_config()

    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    await asyncio.to_thread(command.upgrade, cfg, "head")
