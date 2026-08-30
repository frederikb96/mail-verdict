"""
postimap_commands -- the one PG NOTIFY channel MailVerdict sends on.

The channel and envelope shape (v, action) are stable per the contract;
more actions may be added in later contract versions without breaking
existing senders, the same way postimap_events is extensible by type.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection


async def request_sync(session: AsyncSession, account_id: uuid.UUID) -> None:
    """
    Ask PostIMAP for an immediate incremental sync of an account, without
    waiting for its next periodic cycle.

    Args:
        session: Active AsyncSession (caller commits)
        account_id: Account to sync now
    """
    payload = json.dumps({"v": 1, "action": "sync", "account_id": str(account_id)})
    await session.execute(
        text("SELECT pg_notify('postimap_commands', :payload)"),
        {"payload": payload},
    )


async def request_sync_now(db: DatabaseConnection, account_id: uuid.UUID) -> None:
    """
    Convenience wrapper: request_sync in its own committed session.

    Args:
        db: Database connection
        account_id: Account to sync now
    """
    async with db.session() as session:
        await request_sync(session, account_id)
