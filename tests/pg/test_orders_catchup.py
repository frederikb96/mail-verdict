"""
The catch-up sweep (orders/catch_up.py) against a real database: a dry
run counts what would be queued without creating anything, a real run
creates order_jobs from mail already sitting in the mailbox, oldest
first, and a mail once removed from an order stays out even once the
window widens to cover it again -- the durable order_jobs row a catch-up
never re-reads left behind by the earlier pipeline run that queued it.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import OrderJob, OrderMail
from mail_verdict.orders import repository
from mail_verdict.orders.catch_up import run_catch_up
from mail_verdict.settings.service import SettingsService

pytestmark = pytest.mark.asyncio

_imap_uid_counter = itertools.count(1)
_NOW = datetime.now(timezone.utc)
_FILTER = {"include": {"subject": ["order"]}}


async def _settings_service(db: DatabaseConnection) -> SettingsService:
    service = SettingsService(db)
    await service.load()
    await service.update("orders", {"filter": _FILTER})
    return service


async def _seed_account_and_folder(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    account_id = uuid.uuid4()
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text("INSERT INTO folders (id, account_id, imap_name) VALUES (:id, :account_id, 'INBOX')"),
        {"id": folder_id, "account_id": account_id},
    )
    return account_id, folder_id


async def _seed_message(
    session: AsyncSession, *, account_id: uuid.UUID, folder_id: uuid.UUID,
    subject: str, received_at: datetime,
) -> tuple[uuid.UUID, str]:
    mail_id = uuid.uuid4()
    header = f"<{uuid.uuid4()}@example.com>"
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, from_addr, "
            "subject, body_text, received_at, size_bytes, is_seen) "
            "VALUES (:id, :account_id, :folder_id, :uid, :thread_id, :message_id, :from_addr, "
            ":subject, 'body', :received_at, 512, false)"
        ),
        {
            "id": mail_id, "account_id": account_id, "folder_id": folder_id,
            "uid": next(_imap_uid_counter), "thread_id": uuid.uuid4(),
            "message_id": header, "from_addr": "shop@example.com", "subject": subject,
            "received_at": received_at,
        },
    )
    return mail_id, header


async def test_dry_run_counts_without_creating_anything(migrated_db: DatabaseConnection) -> None:
    settings_service = await _settings_service(migrated_db)
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        await _seed_message(
            session, account_id=account_id, folder_id=folder_id,
            subject="Your order is confirmed", received_at=_NOW - timedelta(days=1),
        )

    result = await run_catch_up(
        migrated_db, settings_service, account_id=account_id, days=7, dry_run=True,
    )

    assert result.considered == 1
    assert result.passed == 1
    assert result.queued == 0
    async with migrated_db.session() as session:
        # Scoped to this test's own account -- migrated_db is shared
        # across the whole pg-layer session, so other tests' order_jobs
        # rows are already sitting in the same table.
        jobs = (
            await session.execute(select(OrderJob).where(OrderJob.account_id == account_id))
        ).scalars().all()
    assert jobs == []


async def test_a_real_run_creates_jobs_oldest_first(migrated_db: DatabaseConnection) -> None:
    settings_service = await _settings_service(migrated_db)
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        oldest = _NOW - timedelta(days=3)
        middle = _NOW - timedelta(days=2)
        newest = _NOW - timedelta(days=1)
        # Inserted out of chronological order on purpose -- the sweep's
        # own ORDER BY received_at is what has to produce the order, not
        # insertion order.
        await _seed_message(
            session, account_id=account_id, folder_id=folder_id,
            subject="Your order shipped", received_at=newest,
        )
        await _seed_message(
            session, account_id=account_id, folder_id=folder_id,
            subject="Your order confirmed", received_at=oldest,
        )
        await _seed_message(
            session, account_id=account_id, folder_id=folder_id,
            subject="Your order is out for delivery", received_at=middle,
        )

    result = await run_catch_up(
        migrated_db, settings_service, account_id=account_id, days=7, dry_run=False,
    )
    assert result.considered == 3
    assert result.passed == 3
    assert result.queued == 3

    async with migrated_db.session() as session:
        jobs = (
            await session.execute(
                select(OrderJob).where(OrderJob.account_id == account_id).order_by(
                    OrderJob.next_attempt_at,
                )
            )
        ).scalars().all()
    assert [j.origin for j in jobs] == ["catchup", "catchup", "catchup"]
    assert [j.next_attempt_at for j in jobs] == [oldest, middle, newest]


async def test_a_mail_removed_from_an_order_stays_out_even_once_the_window_widens(
    migrated_db: DatabaseConnection,
) -> None:
    settings_service = await _settings_service(migrated_db)
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        old_enough = _NOW - timedelta(days=10)
        mail_id, msg_key = await _seed_message(
            session, account_id=account_id, folder_id=folder_id,
            subject="Your order confirmed", received_at=old_enough,
        )
        # Stand in for the pipeline having already queued and processed
        # this mail once, attached it, and it then being removed by hand
        # (api/orders.py's detach_mail: the job row is left as-is on
        # purpose so a catch-up never reconsiders it).
        order_id = await repository.create_order(session)
        await repository.enqueue_mail_job(
            session, account_id=account_id, msg_key=msg_key, message_id=mail_id,
            origin="live", priority=0, filter_reason="subject", next_attempt_at=old_enough,
        )
        await repository.attach_mail(
            session, order_id=order_id, account_id=account_id, msg_key=msg_key,
            message_id=mail_id, thread_id=None, subject="Your order confirmed",
            from_addr="shop@example.com", received_at=old_enough, attached_by="ai",
        )
        await repository.detach_mail(
            session,
            (
                await session.execute(
                    select(OrderMail.id).where(OrderMail.account_id == account_id)
                )
            ).scalar_one(),
        )

    # A window wide enough to reach the mail, run for real this time.
    result = await run_catch_up(
        migrated_db, settings_service, account_id=account_id, days=30, dry_run=False,
    )

    assert result.considered == 1
    assert result.passed == 0  # excluded before the filter even runs -- already queued
    assert result.queued == 0
    async with migrated_db.session() as session:
        jobs = (
            await session.execute(
                select(OrderJob).where(
                    OrderJob.account_id == account_id, OrderJob.msg_key == msg_key,
                )
            )
        ).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].origin == "live"
