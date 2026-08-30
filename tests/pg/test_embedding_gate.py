"""
The embedding-first gate: a message is embedded before it can ever reach
the pipeline queue -- proven end to end against a real Postgres.

enqueue_live_arrival enqueues an embedding, never a pipeline run directly.
Only the embedding's own terminal transition (write_result on success,
fail on a permanent failure) opens the second gate, in the same
transaction as that transition. Reconciliation respects the same gate.

pipeline_runs is shared, session-wide state -- see _delete_pipeline_runs:
any test proving the gate opens leaves a genuine pending row behind, and
another pg test's own claim_batch(batch_size=1) elsewhere would otherwise
be free to claim it instead of the row it just inserted itself.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import EMBEDDING_DIMENSIONS, MessageEmbedding
from mail_verdict.embeddings.repository import EmbeddingRepository
from mail_verdict.pipeline.enqueue import _reconcile_once, enqueue_live_arrival
from mail_verdict.postimap.listener import PostimapEvent
from mail_verdict.queue.work_queue import WorkQueue
from mail_verdict.settings.service import SettingsService

_imap_uid_counter = itertools.count(1)


def _unique_model() -> str:
    """migrated_db is shared across the file's tests -- see
    tests/pg/test_message_embeddings.py's identical helper for why."""
    return f"model-{uuid.uuid4().hex[:8]}"


async def _seed_synced_account_and_inbox(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """A folder whose backfill has already completed -- live-eligible for
    the pipeline once a message inside it also has a terminal embedding.

    Returns:
        (account_id, folder_id)
    """
    account_id, folder_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, initial_sync_done) "
            "VALUES (:id, :account_id, 'INBOX', true)"
        ),
        {"id": folder_id, "account_id": account_id},
    )
    await session.execute(
        text(
            "INSERT INTO pipeline_folder_state (folder_id, account_id, backfill_completed_at) "
            "VALUES (:folder_id, :account_id, now() - interval '1 hour')"
        ),
        {"folder_id": folder_id, "account_id": account_id},
    )
    return account_id, folder_id


async def _seed_message(
    session: AsyncSession, *, account_id: uuid.UUID, folder_id: uuid.UUID,
    subject: str = "Hello",
) -> uuid.UUID:
    """A message that arrived after the folder's watermark -- live-eligible
    on its own, pending only the embedding gate."""
    mail_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, from_addr, "
            "subject, body_text, received_at, size_bytes, created_at) "
            "VALUES (:id, :account_id, :folder_id, :imap_uid, :thread_id, :message_id, "
            "'sender@example.com', :subject, 'Body.', now(), 1024, now())"
        ),
        {
            "id": mail_id, "account_id": account_id, "folder_id": folder_id,
            "imap_uid": next(_imap_uid_counter), "thread_id": uuid.uuid4(),
            "message_id": f"<{uuid.uuid4()}@example.com>", "subject": subject,
        },
    )
    return mail_id


async def _settings(db: DatabaseConnection) -> SettingsService:
    service = SettingsService(db)
    await service.load()
    return service


async def _delete_pipeline_runs(db: DatabaseConnection, account_id: uuid.UUID) -> None:
    """A test proving the gate opens leaves a genuine 'pending' row in
    pipeline_runs behind -- pipeline_runs is shared, unreset, session-wide
    state (see the module docstring), and another pg test's
    claim_batch(batch_size=1) elsewhere would otherwise claim this
    leftover row instead of the one it just inserted itself. Scoped by
    account_id, unique per test, so this can never touch another test's
    rows."""
    async with db.session() as session:
        await session.execute(
            text("DELETE FROM pipeline_runs WHERE account_id = :a"), {"a": account_id},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_live_arrival_enqueues_an_embedding_not_a_pipeline_run(
    migrated_db: DatabaseConnection,
) -> None:
    """The first half of the gate: arrival never inserts into
    pipeline_runs directly, only into message_embeddings."""
    settings_service = await _settings(migrated_db)
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_synced_account_and_inbox(session)
        mail_id = await _seed_message(session, account_id=account_id, folder_id=folder_id)
        await session.commit()

    event = PostimapEvent(
        v=1, type="message", op="insert", id=str(mail_id), account_id=str(account_id),
        origin="sync",
    )
    await enqueue_live_arrival(migrated_db, event, settings_service)

    async with migrated_db.session() as session:
        embedding_row = (
            await session.execute(
                text(
                    "SELECT status, priority FROM message_embeddings "
                    "WHERE account_id = :a AND message_id = :m"
                ),
                {"a": account_id, "m": mail_id},
            )
        ).one()
        run_count = (
            await session.execute(
                text("SELECT count(*) FROM pipeline_runs WHERE account_id = :a"),
                {"a": account_id},
            )
        ).scalar_one()

    assert embedding_row.status == "pending"
    assert embedding_row.priority == 0
    assert run_count == 0


@pytest.mark.asyncio
async def test_embedding_success_opens_the_pipeline_gate(
    migrated_db: DatabaseConnection,
) -> None:
    """Once the embedding write_result lands, a pipeline run appears for
    the same message -- in the same call, no separate reconciliation
    pass needed."""
    settings_service = await _settings(migrated_db)
    embedding_repo = EmbeddingRepository(migrated_db)
    model = _unique_model()

    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_synced_account_and_inbox(session)
        mail_id = await _seed_message(session, account_id=account_id, folder_id=folder_id)
        await session.commit()

    await embedding_repo.enqueue_one(account_id=account_id, message_id=mail_id, model=model)

    work_queue = WorkQueue(migrated_db, MessageEmbedding.__table__)
    claimed = await work_queue.claim_batch(worker_id="w1", batch_size=10, lease_seconds=30)
    item = next(row for row in claimed if row["message_id"] == mail_id)

    wrote = await embedding_repo.write_result(
        item["id"], worker_id="w1", embedding=[0.1] * EMBEDDING_DIMENSIONS, model=model,
        content_level="full", source_hash="abc", settings_service=settings_service,
    )
    assert wrote is True

    async with migrated_db.session() as session:
        run = (
            await session.execute(
                text(
                    "SELECT origin, dedup_key, status FROM pipeline_runs "
                    "WHERE account_id = :a AND message_id = :m"
                ),
                {"a": account_id, "m": mail_id},
            )
        ).one()
    assert run.origin == "live"
    assert run.dedup_key == "live"
    assert run.status == "pending"

    await _delete_pipeline_runs(migrated_db, account_id)


@pytest.mark.asyncio
async def test_permanently_failed_embedding_still_opens_the_pipeline_gate(
    migrated_db: DatabaseConnection,
) -> None:
    """A message that can never be embedded is not stranded -- reaching
    'failed' opens the same gate 'done' does."""
    settings_service = await _settings(migrated_db)
    embedding_repo = EmbeddingRepository(migrated_db)
    model = _unique_model()

    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_synced_account_and_inbox(session)
        mail_id = await _seed_message(session, account_id=account_id, folder_id=folder_id)
        await session.commit()

    await embedding_repo.enqueue_one(account_id=account_id, message_id=mail_id, model=model)

    work_queue = WorkQueue(migrated_db, MessageEmbedding.__table__)
    claimed = await work_queue.claim_batch(worker_id="w1", batch_size=10, lease_seconds=30)
    item = next(row for row in claimed if row["message_id"] == mail_id)

    ok = await embedding_repo.fail(
        item["id"], worker_id="w1", last_error="provider rejected this content permanently",
        settings_service=settings_service,
    )
    assert ok is True

    async with migrated_db.session() as session:
        embedding_status = (
            await session.execute(
                text("SELECT status FROM message_embeddings WHERE id = :id"), {"id": item["id"]},
            )
        ).scalar_one()
        run = (
            await session.execute(
                text(
                    "SELECT origin FROM pipeline_runs WHERE account_id = :a AND message_id = :m"
                ),
                {"a": account_id, "m": mail_id},
            )
        ).one_or_none()

    assert embedding_status == "failed"
    assert run is not None
    assert run.origin == "live"

    await _delete_pipeline_runs(migrated_db, account_id)


@pytest.mark.asyncio
async def test_historical_backfill_embedding_never_opens_the_pipeline_gate(
    migrated_db: DatabaseConnection,
) -> None:
    """A message in a folder with no watermark yet (or arriving before
    one) is historical -- its embedding reaching 'done' must never create
    a pipeline run on its own; only a deliberate sweep may."""
    settings_service = await _settings(migrated_db)
    embedding_repo = EmbeddingRepository(migrated_db)
    model = _unique_model()

    account_id, folder_id = uuid.uuid4(), uuid.uuid4()
    async with migrated_db.session() as session:
        await session.execute(
            text(
                "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, "
                "imap_password) VALUES (:id, :name, 'imap.example.com', 993, "
                "'user@example.com', '\\x00' || convert_to('pw', 'UTF8'))"
            ),
            {"id": account_id, "name": f"acct-{account_id}"},
        )
        # No pipeline_folder_state row at all: this folder has never
        # finished a first sync as far as MailVerdict's own watermark is
        # concerned, however initial_sync_done reads on PostIMAP's side.
        await session.execute(
            text(
                "INSERT INTO folders (id, account_id, imap_name, initial_sync_done) "
                "VALUES (:id, :account_id, 'INBOX', true)"
            ),
            {"id": folder_id, "account_id": account_id},
        )
        mail_id = await _seed_message(session, account_id=account_id, folder_id=folder_id)
        await session.commit()

    await embedding_repo.enqueue_one(account_id=account_id, message_id=mail_id, model=model)

    work_queue = WorkQueue(migrated_db, MessageEmbedding.__table__)
    claimed = await work_queue.claim_batch(worker_id="w1", batch_size=10, lease_seconds=30)
    item = next(row for row in claimed if row["message_id"] == mail_id)

    await embedding_repo.write_result(
        item["id"], worker_id="w1", embedding=[0.1] * EMBEDDING_DIMENSIONS, model=model,
        content_level="full", source_hash="abc", settings_service=settings_service,
    )

    async with migrated_db.session() as session:
        run_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM pipeline_runs WHERE account_id = :a AND message_id = :m"
                ),
                {"a": account_id, "m": mail_id},
            )
        ).scalar_one()
    assert run_count == 0


@pytest.mark.asyncio
async def test_reconciliation_only_enqueues_messages_with_a_terminal_embedding(
    migrated_db: DatabaseConnection,
) -> None:
    """The gap-recovery path (a listener reconnect) must not bypass the
    embedding gate: a live-eligible message with no embedding yet is left
    alone, one with a 'done' embedding is enqueued."""
    settings_service = await _settings(migrated_db)
    model = str(settings_service.get("semantic")["model"])

    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_synced_account_and_inbox(session)
        embedded_id = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, subject="has embedding",
        )
        unembedded_id = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, subject="missing embedding",
        )
        await session.commit()

    async with migrated_db.session() as session:
        await session.execute(
            text(
                "INSERT INTO message_embeddings "
                "(account_id, msg_key, message_id, model, status, embedding) "
                "VALUES (:a, :k, :m, :model, 'done', array_fill(0.1, ARRAY[1536])::vector)"
            ),
            {"a": account_id, "k": str(embedded_id), "m": embedded_id, "model": model},
        )
        await session.commit()

    await _reconcile_once(migrated_db, settings_service)

    async with migrated_db.session() as session:
        runs = (
            await session.execute(
                text("SELECT message_id FROM pipeline_runs WHERE account_id = :a"),
                {"a": account_id},
            )
        ).all()
    run_message_ids = {row.message_id for row in runs}
    assert embedded_id in run_message_ids
    assert unembedded_id not in run_message_ids

    await _delete_pipeline_runs(migrated_db, account_id)


@pytest.mark.asyncio
async def test_live_eligibility_respects_the_max_age_guard(migrated_db: DatabaseConnection) -> None:
    """A message older than pipeline.live_max_age_days is never treated as
    live-eligible, however its folder's watermark reads -- the secondary
    guard against a missing or stale watermark reclassifying old mail."""
    from mail_verdict.pipeline.enqueue import enqueue_pipeline_run_if_live_eligible

    settings_service = await _settings(migrated_db)
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_synced_account_and_inbox(session)
        mail_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, account_id, folder_id, imap_uid, thread_id, message_id, from_addr, "
                "subject, body_text, received_at, size_bytes, created_at) "
                "VALUES (:id, :account_id, :folder_id, :imap_uid, :thread_id, :message_id, "
                "'sender@example.com', 'old mail', 'Body.', :received_at, 1024, now())"
            ),
            {
                "id": mail_id, "account_id": account_id, "folder_id": folder_id,
                "imap_uid": next(_imap_uid_counter), "thread_id": uuid.uuid4(),
                "message_id": f"<{uuid.uuid4()}@example.com>",
                "received_at": datetime.now(timezone.utc) - timedelta(days=30),
            },
        )
        await session.commit()

    async with migrated_db.session() as session:
        inserted = await enqueue_pipeline_run_if_live_eligible(
            session, account_id=account_id, message_id=mail_id, settings_service=settings_service,
        )
        await session.commit()
    assert inserted is False
