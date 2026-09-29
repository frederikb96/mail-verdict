"""
Semantic search over the glacier (design section 4.7): an embedding
whose hint is NULL because its message was glaciered is still found,
still counted reachable in coverage status, and a glacier row missing
a vector entirely still gets one enqueued and encoded -- from its own
stored subject/from/body, never a live `messages` row.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import EMBEDDING_DIMENSIONS, MessageEmbedding
from mail_verdict.database.repository import MessageRepository
from mail_verdict.embeddings.provider import FakeEmbeddingProvider
from mail_verdict.embeddings.repository import EmbeddingRepository
from mail_verdict.embeddings.search import semantic_search
from mail_verdict.embeddings.worker import _handle_one
from mail_verdict.queue.work_queue import WorkQueue

_RAW_SOURCE = b"From: sender@example.com\r\nSubject: Test\r\n\r\nBody\r\n"


def _unique_model() -> str:
    return f"model-{uuid.uuid4().hex[:8]}"


async def _seed_account(session: AsyncSession) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts "
            "(id, name, imap_host, imap_port, imap_user, imap_password, is_active) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'), false)"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    return account_id


async def _enable_glacier(session: AsyncSession, account_id: uuid.UUID) -> uuid.UUID:
    glacier_folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id) "
            "VALUES (:account_id, true, :glacier_folder_id)"
        ),
        {"account_id": account_id, "glacier_folder_id": glacier_folder_id},
    )
    return glacier_folder_id


async def _seed_glacier_message(
    session: AsyncSession, *, account_id: uuid.UUID, glacier_folder_id: uuid.UUID,
    subject: str = "Archived subject", body_text: str = "Archived body",
    received_at: datetime | None = None,
) -> tuple[uuid.UUID, str]:
    """Returns (glacier_id, msg_key)."""
    glacier_id = uuid.uuid4()
    msg_key = f"msg-{glacier_id}"
    await session.execute(
        text(
            "INSERT INTO glacier_messages "
            "(id, account_id, folder_id, thread_id, message_id, subject, from_addr, "
            " body_text, raw_source, size_bytes, received_at, msg_key, state, visible_at, "
            " glaciered_at) "
            "VALUES (:id, :account_id, :folder_id, :thread_id, :message_id_hdr, :subject, "
            " 'sender@example.com', :body_text, :raw_source, :size_bytes, :received_at, "
            " :msg_key, 'glaciered', now(), now())"
        ),
        {
            "id": glacier_id, "account_id": account_id, "folder_id": glacier_folder_id,
            "thread_id": glacier_id, "message_id_hdr": f"<{glacier_id}@example.com>",
            "subject": subject, "body_text": body_text, "raw_source": _RAW_SOURCE,
            "size_bytes": len(_RAW_SOURCE),
            "received_at": received_at or datetime.now(timezone.utc), "msg_key": msg_key,
        },
    )
    return glacier_id, msg_key


class _FakeSettings:
    def __init__(self, **overrides: object) -> None:
        self._values: dict[str, object] = {
            "content_chars": 2000, "provider": "fake", "batch_size": 10,
            "max_attempts": 5, "base_delay_seconds": 0.0, "max_delay_seconds": 0.0,
        }
        self._values.update(overrides)

    def get(self, category: str) -> dict[str, object]:
        return self._values

    def has_category(self, category: str) -> bool:
        return False


class _NullCircuit:
    async def record_success(self) -> None:
        return None

    async def record_unavailable(self, **kwargs: object) -> None:
        return None

    async def record_backoff(self, **kwargs: object) -> None:
        return None


async def _claim_one(db: DatabaseConnection, item_id: uuid.UUID) -> dict[str, object]:
    async with db.session() as session:
        result = await session.execute(
            text(
                "UPDATE message_embeddings SET status = 'claimed', claimed_by = 'w1', "
                "claimed_at = now(), lease_expires_at = now() + interval '30 seconds', "
                "attempts = attempts + 1 WHERE id = :id AND status = 'pending' RETURNING *"
            ),
            {"id": item_id},
        )
        row = result.mappings().one()
        return dict(row)


@pytest.mark.asyncio
async def test_semantic_search_finds_a_glaciered_message(
    migrated_db: DatabaseConnection,
) -> None:
    """An embedding row whose hint is NULL because its message was
    glaciered is still found, joined by (account_id, msg_key) instead."""
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        glacier_folder_id = await _enable_glacier(session, account_id)
        glacier_id, msg_key = await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
        )
        await session.commit()

    query_vector = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
    close_vector = [0.99] + [0.01] * (EMBEDDING_DIMENSIONS - 1)
    async with migrated_db.session() as session:
        await session.execute(
            MessageEmbedding.__table__.insert().values(
                account_id=account_id, msg_key=msg_key, message_id=None,
                model=model, status="done", embedding=close_vector,
            )
        )

    outcome = await semantic_search(
        migrated_db, query_vector=query_vector, model=model, account_id=account_id,
        k=10, strictness="loose",
    )
    assert len(outcome.results) == 1
    assert outcome.results[0].message.id == glacier_id


@pytest.mark.asyncio
async def test_semantic_search_scoped_to_nothing_still_finds_it(
    migrated_db: DatabaseConnection,
) -> None:
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        glacier_folder_id = await _enable_glacier(session, account_id)
        glacier_id, msg_key = await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
        )
        await session.commit()

    query_vector = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
    close_vector = [0.99] + [0.01] * (EMBEDDING_DIMENSIONS - 1)
    async with migrated_db.session() as session:
        await session.execute(
            MessageEmbedding.__table__.insert().values(
                account_id=account_id, msg_key=msg_key, message_id=None,
                model=model, status="done", embedding=close_vector,
            )
        )

    outcome = await semantic_search(
        migrated_db, query_vector=query_vector, model=model, account_id=None,
        k=10, strictness="loose",
    )
    assert glacier_id in {r.message.id for r in outcome.results}


@pytest.mark.asyncio
async def test_status_counts_a_null_hint_glacier_row_as_reachable(
    migrated_db: DatabaseConnection,
) -> None:
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        glacier_folder_id = await _enable_glacier(session, account_id)
        _glacier_id, msg_key = await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
        )
        await session.commit()

    vector = [0.5] * EMBEDDING_DIMENSIONS
    async with migrated_db.session() as session:
        await session.execute(
            MessageEmbedding.__table__.insert().values(
                account_id=account_id, msg_key=msg_key, message_id=None,
                model=model, status="done", embedding=vector,
            )
        )

    embedding_repo = EmbeddingRepository(migrated_db)
    status = await embedding_repo.status(model=model, account_id=account_id)
    assert status.encoded == 1
    assert status.reachable == 1
    assert status.unreachable == 0
    assert status.in_scope == 1


@pytest.mark.asyncio
async def test_enqueue_missing_batch_enqueues_a_visible_glacier_row(
    migrated_db: DatabaseConnection,
) -> None:
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        glacier_folder_id = await _enable_glacier(session, account_id)
        _glacier_id, msg_key = await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
        )
        await session.commit()

    embedding_repo = EmbeddingRepository(migrated_db)
    seen, inserted = await embedding_repo.enqueue_missing_batch(
        model=model, batch_size=50, account_id=account_id,
    )
    assert seen == 1
    assert inserted == 1

    async with migrated_db.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT message_id, status FROM message_embeddings "
                    "WHERE account_id = :account_id AND msg_key = :msg_key AND model = :model"
                ),
                {"account_id": account_id, "msg_key": msg_key, "model": model},
            )
        ).mappings().one()
    assert row["message_id"] is None
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_worker_embeds_a_glaciered_message_from_its_own_stored_fields(
    migrated_db: DatabaseConnection,
) -> None:
    """The full path: enqueue a visible glacier row, claim it, run the
    worker's own _handle_one -- content comes from the glacier row's
    stored subject/body, never a `messages` row (there is none)."""
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        glacier_folder_id = await _enable_glacier(session, account_id)
        glacier_id, msg_key = await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            subject="Findable glacier subject", body_text="Findable glacier body",
        )
        await session.commit()

    embedding_repo = EmbeddingRepository(migrated_db)
    message_repo = MessageRepository(migrated_db)
    await embedding_repo.enqueue_missing_batch(model=model, batch_size=50, account_id=account_id)

    async with migrated_db.session() as session:
        item_id = (
            await session.execute(
                text(
                    "SELECT id FROM message_embeddings "
                    "WHERE account_id = :account_id AND msg_key = :msg_key"
                ),
                {"account_id": account_id, "msg_key": msg_key},
            )
        ).scalar_one()
    claimed = await _claim_one(migrated_db, item_id)

    work_queue = WorkQueue(migrated_db, MessageEmbedding.__table__)

    import mail_verdict.embeddings.worker as worker_module

    original_resolve = worker_module.resolve_embedding_provider
    worker_module.resolve_embedding_provider = lambda *a, **k: FakeEmbeddingProvider()  # type: ignore[assignment]
    try:
        await _handle_one(
            claimed, "w1", work_queue, embedding_repo, message_repo,
            cred_repo=None, settings_service=_FakeSettings(),  # type: ignore[arg-type]
            circuit=_NullCircuit(), db=migrated_db,  # type: ignore[arg-type]
        )
    finally:
        worker_module.resolve_embedding_provider = original_resolve

    async with migrated_db.session() as session:
        final = (
            await session.execute(
                text("SELECT status, embedding, message_id FROM message_embeddings WHERE id = :id"),
                {"id": item_id},
            )
        ).mappings().one()
    assert final["status"] == "done"
    assert final["embedding"] is not None
    assert final["message_id"] is None

    status = await embedding_repo.status(model=model, account_id=account_id)
    assert status.reachable == 1
    assert status.unreachable == 0
