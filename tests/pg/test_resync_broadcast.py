"""
broadcast_resync: the listener-reconnect safeguard that pushes a resync
event to every open stream, not only the one whose own SSE connection
happens to reconnect later and replay a stale Last-Event-ID.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from mail_verdict.api.event_ring import EventRing
from mail_verdict.api.events import broadcast_resync
from mail_verdict.database.connection import DatabaseConnection
from tests.helpers.auth import user_scope


async def _seed_account(db: DatabaseConnection) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
                "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
                "'\\x00' || convert_to('pw', 'UTF8'))"
            ),
            {"id": account_id, "name": f"acct-{account_id}"},
        )
        await session.commit()
    return account_id


@pytest.mark.asyncio
async def test_pushes_a_resync_event_to_every_stream(migrated_db: DatabaseConnection) -> None:
    """Every user's stream gets the resync -- a currently-connected browser
    of either one must see it on its live SSE stream, not only a browser
    that happens to reconnect afterward."""
    account_a = await _seed_account(migrated_db)
    account_b = await _seed_account(migrated_db)
    ring = EventRing()

    await broadcast_resync(migrated_db, ring)

    for scope in (user_scope(uuid.uuid4(), [account_a]), user_scope(uuid.uuid4(), [account_b])):
        events = await ring.replay_from(0, scope.ring_keys())
        assert [event["event_type"] for event in events] == ["resync"]
        assert events[0]["data"] == {}


@pytest.mark.asyncio
async def test_does_not_raise_against_a_freshly_constructed_ring(
    migrated_db: DatabaseConnection,
) -> None:
    """A ring with no prior events for any account is still a valid
    target -- a fresh instance's listener can reconnect before any
    account was ever added or any other event ever pushed."""
    ring = EventRing()

    await broadcast_resync(migrated_db, ring)
