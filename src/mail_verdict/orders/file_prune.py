"""
The hourly deletion of files the register failed to place. Placing a file is
retried by the orders queue; one that still failed is kept for the
configured number of days (so the failure can be looked at) and then removed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mail_verdict.orders import files
from mail_verdict.queue.notify import ReconciliationTimer

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

# test_lock_keys.py asserts every *_LOCK_KEY constant in the package is
# pairwise distinct.
ORDER_FILES_PRUNE_LOCK_KEY = 761_035_400

_PRUNE_INTERVAL_SECONDS = 3600.0


def build_file_prune_timer(db: DatabaseConnection, retention_days: int) -> ReconciliationTimer:
    """The advisory-locked hourly pass that deletes failed files past
    their retention -- one per process, safe with more than one replica."""

    async def _callback() -> None:
        async with db.session() as session:
            deleted = await files.prune_failed_files(session, days=retention_days)
        if deleted:
            logger.info("Deleted %d failed order files", deleted)

    return ReconciliationTimer(
        db, ORDER_FILES_PRUNE_LOCK_KEY, _callback, _PRUNE_INTERVAL_SECONDS,
    )
