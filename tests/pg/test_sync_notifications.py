"""
The notification centre: SyncNotificationRepository, the acknowledge
actions, and the API endpoints, against a real Postgres schema carrying
PostIMAP's own sync_notifications table and grants.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.notifications import (
    acknowledge,
    acknowledge_all,
    get_unacknowledged_count,
    list_notifications,
)
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.repository import SyncNotificationRepository
from mail_verdict.postimap.actions import acknowledge_all_notifications, acknowledge_notification


async def _seed_account(session: AsyncSession) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    return account_id


async def _seed_notification(
    session: AsyncSession, *, account_id: uuid.UUID, action: str = "flag_add",
    error: str = "NO [CANNOT] Invalid flag",
) -> int:
    result = await session.execute(
        text(
            "INSERT INTO sync_notifications (account_id, action, error, detail) "
            "VALUES (:account_id, :action, :error, '{}'::jsonb) RETURNING id"
        ),
        {"account_id": account_id, "action": action, "error": error},
    )
    return int(result.scalar_one())


@pytest.mark.asyncio
async def test_repository_lists_newest_first(migrated_db: DatabaseConnection) -> None:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        first_id = await _seed_notification(session, account_id=account_id, action="flag_add")
        second_id = await _seed_notification(session, account_id=account_id, action="move")

    repo = SyncNotificationRepository(migrated_db)
    rows = await repo.list_for_account(account_id)

    assert [r.id for r in rows] == [second_id, first_id]


@pytest.mark.asyncio
async def test_repository_unacknowledged_only_filter(migrated_db: DatabaseConnection) -> None:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        acked_id = await _seed_notification(session, account_id=account_id)
        unacked_id = await _seed_notification(session, account_id=account_id)
        await acknowledge_notification(session, acked_id)

    repo = SyncNotificationRepository(migrated_db)
    unacked = await repo.list_for_account(account_id, unacknowledged_only=True)

    assert [r.id for r in unacked] == [unacked_id]
    assert await repo.unacknowledged_count(account_id) == 1


@pytest.mark.asyncio
async def test_acknowledge_all_clears_every_unacknowledged_row(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        await _seed_notification(session, account_id=account_id)
        await _seed_notification(session, account_id=account_id)

    async with migrated_db.session() as session:
        await acknowledge_all_notifications(session, account_id)

    repo = SyncNotificationRepository(migrated_db)
    assert await repo.unacknowledged_count(account_id) == 0


@pytest.mark.asyncio
async def test_api_list_and_ack_endpoints(migrated_db: DatabaseConnection) -> None:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        notification_id = await _seed_notification(
            session, account_id=account_id, action="send",
            error="SMTP 550 relay not permitted",
        )

    listed = await list_notifications(account_id, unacknowledged_only=False, limit=100)
    assert len(listed) == 1
    assert listed[0].id == notification_id
    assert listed[0].action == "send"
    assert listed[0].acknowledged_at is None

    count = await get_unacknowledged_count(account_id)
    assert count.unacknowledged == 1

    await acknowledge(account_id, notification_id)

    count_after = await get_unacknowledged_count(account_id)
    assert count_after.unacknowledged == 0


@pytest.mark.asyncio
async def test_api_ack_all_endpoint(migrated_db: DatabaseConnection) -> None:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        await _seed_notification(session, account_id=account_id)
        await _seed_notification(session, account_id=account_id)

    await acknowledge_all(account_id)

    count = await get_unacknowledged_count(account_id)
    assert count.unacknowledged == 0
