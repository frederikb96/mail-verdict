"""
message_embeddings: the vector extension, the self-advancing backfill
predicate, coverage status, semantic search ordering, and the worker's
read -> embed -> write path against a real Postgres.
"""

from __future__ import annotations

import itertools
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
from mail_verdict.settings.service import SettingsService

_imap_uid_counter = itertools.count(1)


def _unique_model() -> str:
    """A model name unique to one test call.

    migrated_db is not reset between tests in this suite -- rows from an
    earlier test's account are still visible to a later one -- so every
    test that queries by model uses its own, never a constant shared
    across the file.
    """
    return f"model-{uuid.uuid4().hex[:8]}"


async def _seed_account_and_folder(
    session: AsyncSession, *, synced: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a minimal account + folder, return (account_id, folder_id)."""
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
    if synced:
        await session.execute(
            text("UPDATE folders SET initial_sync_done = true WHERE id = :id"),
            {"id": folder_id},
        )
    return account_id, folder_id


async def _seed_message(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    folder_id: uuid.UUID,
    message_id_hdr: str | None = None,
    subject: str = "Hello",
    from_addr: str = "sender@example.com",
    body_text: str | None = "Body text.",
    body_html: str | None = None,
    is_truncated: bool = False,
    expunged: bool = False,
) -> uuid.UUID:
    """Insert a minimal message row, return its id."""
    mail_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, from_addr, "
            "subject, body_text, body_html, is_truncated, received_at, size_bytes, "
            "expunged_at) "
            "VALUES (:id, :account_id, :folder_id, :imap_uid, :thread_id, :message_id, "
            ":from_addr, :subject, :body_text, :body_html, :is_truncated, :received_at, "
            "1024, :expunged_at)"
        ),
        {
            "id": mail_id, "account_id": account_id, "folder_id": folder_id,
            "imap_uid": next(_imap_uid_counter),
            "thread_id": uuid.uuid4(), "message_id": message_id_hdr, "from_addr": from_addr,
            "subject": subject, "body_text": body_text, "body_html": body_html,
            "is_truncated": is_truncated,
            "received_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "expunged_at": datetime(2026, 1, 2, tzinfo=timezone.utc) if expunged else None,
        },
    )
    return mail_id


@pytest.mark.asyncio
async def test_vector_extension_is_installed(migrated_db: DatabaseConnection) -> None:
    """The migration must have created the extension -- every other test in
    this file depends on it silently; this one asserts it directly."""
    async with migrated_db.session() as session:
        result = await session.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        )
        assert result.scalar_one_or_none() == 1


@pytest.mark.asyncio
async def test_message_embeddings_satisfies_the_queue_engine(
    migrated_db: DatabaseConnection,
) -> None:
    """message_embeddings carries every column WorkQueue requires -- proven
    by construction succeeding rather than by listing columns twice."""
    WorkQueue(migrated_db, MessageEmbedding.__table__)


@pytest.mark.asyncio
async def test_enqueue_missing_batch_finds_in_scope_messages(
    migrated_db: DatabaseConnection,
) -> None:
    """A message in a synced, non-expunged folder gets a pending row."""
    repo = EmbeddingRepository(migrated_db)
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        mail_id = await _seed_message(session, account_id=account_id, folder_id=folder_id)
        await session.commit()

    candidates, inserted = await repo.enqueue_missing_batch(
        model=model, batch_size=50, account_id=account_id,
    )
    assert candidates == 1
    assert inserted == 1

    async with migrated_db.session() as session:
        row = (
            await session.execute(
                text("SELECT status, message_id FROM message_embeddings WHERE model = :m"),
                {"m": model},
            )
        ).one()
    assert row.status == "pending"
    assert row.message_id == mail_id


@pytest.mark.asyncio
async def test_enqueue_missing_batch_excludes_unsynced_folder(
    migrated_db: DatabaseConnection,
) -> None:
    """A folder still on its first sync is not a source of embeddable
    messages -- the same watermark PostIMAP itself exposes for this."""
    repo = EmbeddingRepository(migrated_db)
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session, synced=False)
        await _seed_message(session, account_id=account_id, folder_id=folder_id)
        await session.commit()

    candidates, inserted = await repo.enqueue_missing_batch(
        model=_unique_model(), batch_size=50, account_id=account_id,
    )
    assert candidates == 0
    assert inserted == 0


@pytest.mark.asyncio
async def test_enqueue_missing_batch_excludes_expunged_messages(
    migrated_db: DatabaseConnection,
) -> None:
    """An expunged message is gone, not a search result waiting to happen."""
    repo = EmbeddingRepository(migrated_db)
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        await _seed_message(session, account_id=account_id, folder_id=folder_id, expunged=True)
        await session.commit()

    candidates, inserted = await repo.enqueue_missing_batch(
        model=_unique_model(), batch_size=50, account_id=account_id,
    )
    assert candidates == 0
    assert inserted == 0


@pytest.mark.asyncio
async def test_enqueue_missing_batch_is_idempotent(migrated_db: DatabaseConnection) -> None:
    """Calling it again after everything is already covered inserts nothing
    -- this is what makes the reconciler safe to run on a timer forever."""
    repo = EmbeddingRepository(migrated_db)
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        await _seed_message(session, account_id=account_id, folder_id=folder_id)
        await session.commit()

    await repo.enqueue_missing_batch(model=model, batch_size=50, account_id=account_id)
    candidates, inserted = await repo.enqueue_missing_batch(
        model=model, batch_size=50, account_id=account_id,
    )
    assert candidates == 0
    assert inserted == 0


@pytest.mark.asyncio
async def test_resync_repoints_the_join_hint_instead_of_duplicating(
    migrated_db: DatabaseConnection,
) -> None:
    """A UIDVALIDITY resync recreates a message under a new id but the same
    Message-ID header. The next enqueue call must not insert a second
    embedding row for the same durable identity -- it repoints the
    existing row's message_id hint instead."""
    repo = EmbeddingRepository(migrated_db)
    model = _unique_model()
    header = f"<{uuid.uuid4()}@example.com>"
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        old_id = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, message_id_hdr=header,
        )
        await session.commit()

    await repo.enqueue_missing_batch(model=model, batch_size=50, account_id=account_id)

    # Simulate the resync: the old row is gone (as if purged after a
    # UIDVALIDITY change), a new row with a new id carries the same header.
    async with migrated_db.session() as session:
        await session.execute(text("DELETE FROM messages WHERE id = :id"), {"id": old_id})
        new_id = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, message_id_hdr=header,
        )
        await session.commit()

    candidates, inserted = await repo.enqueue_missing_batch(
        model=model, batch_size=50, account_id=account_id,
    )
    assert inserted == 0  # the durable key already has a row; nothing new inserted

    async with migrated_db.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT message_id FROM message_embeddings "
                    "WHERE model = :m AND account_id = :a"
                ),
                {"m": model, "a": account_id},
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].message_id == new_id  # the hint now points at the resynced row

    # And the predicate has genuinely advanced: a further call finds nothing new.
    candidates, inserted = await repo.enqueue_missing_batch(
        model=model, batch_size=50, account_id=account_id,
    )
    assert inserted == 0


@pytest.mark.asyncio
async def test_status_reports_coverage(migrated_db: DatabaseConnection) -> None:
    """in_scope/encoded/pending/failed and the derived coverage ratio."""
    repo = EmbeddingRepository(migrated_db)
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        # Distinct subjects: with no Message-ID header, msg_key is a hash
        # of the envelope (msg_key.py) -- identical envelopes collapse to
        # one durable identity by design, so four otherwise-identical rows
        # would only ever produce one embedding.
        for i in range(4):
            await _seed_message(
                session, account_id=account_id, folder_id=folder_id, subject=f"Hello {i}",
            )
        await session.commit()

    await repo.enqueue_missing_batch(model=model, batch_size=50, account_id=account_id)
    status = await repo.status(model=model, account_id=account_id)
    assert status.in_scope == 4
    assert status.pending == 4
    assert status.encoded == 0
    assert status.coverage == 0.0


@pytest.mark.asyncio
async def test_model_change_drops_coverage_to_zero_without_deleting(
    migrated_db: DatabaseConnection,
) -> None:
    """Rows under an old model are never counted toward a new model's
    coverage, and switching models does not delete them -- coverage
    visibly drops rather than silently mixing two vector spaces."""
    repo = EmbeddingRepository(migrated_db)
    old_model, new_model = _unique_model(), _unique_model()
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        await _seed_message(session, account_id=account_id, folder_id=folder_id)
        await session.commit()

    await repo.enqueue_missing_batch(model=old_model, batch_size=50, account_id=account_id)
    old_status = await repo.status(model=old_model, account_id=account_id)
    assert old_status.pending == 1

    new_status = await repo.status(model=new_model, account_id=account_id)
    assert new_status.pending == 0
    assert new_status.encoded == 0
    assert new_status.in_scope == 1  # the message itself is still in scope

    async with migrated_db.session() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM message_embeddings WHERE account_id = :a"),
                {"a": account_id},
            )
        ).scalar_one()
    assert count == 1  # the old row was never deleted


@pytest.mark.asyncio
async def test_write_result_is_guarded_against_a_lost_claim(
    migrated_db: DatabaseConnection,
) -> None:
    """A write from a worker that no longer holds the claim (reclaimed
    after its lease expired) must not land."""
    repo = EmbeddingRepository(migrated_db)
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        await _seed_message(session, account_id=account_id, folder_id=folder_id)
        await session.commit()
    await repo.enqueue_missing_batch(model=model, batch_size=50, account_id=account_id)

    work_queue = WorkQueue(migrated_db, MessageEmbedding.__table__)
    claimed = await work_queue.claim_batch(worker_id="worker-a", batch_size=1, lease_seconds=30)
    item_id = claimed[0]["id"]

    # Simulate a lease reclaim followed by another worker's claim.
    async with migrated_db.session() as session:
        await session.execute(
            text(
                "UPDATE message_embeddings SET claimed_by = 'worker-b' WHERE id = :id"
            ),
            {"id": item_id},
        )

    settings_service = SettingsService(migrated_db)
    await settings_service.load()
    wrote = await repo.write_result(
        item_id, worker_id="worker-a", embedding=[0.1] * EMBEDDING_DIMENSIONS,
        model=model, content_level="full", source_hash="abc",
        settings_service=settings_service,
    )
    assert wrote is False


@pytest.mark.asyncio
async def test_worker_embeds_a_truncated_message_as_envelope_only(
    migrated_db: DatabaseConnection,
) -> None:
    """End-to-end through the worker's own handler: a truncated message is
    still embedded, recorded as content_level='envelope'."""
    embedding_repo = EmbeddingRepository(migrated_db)
    message_repo = MessageRepository(migrated_db)
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        mail_id = await _seed_message(
            session, account_id=account_id, folder_id=folder_id,
            body_text="should be ignored", is_truncated=True,
        )
        await session.commit()
    await embedding_repo.enqueue_missing_batch(model=model, batch_size=50, account_id=account_id)

    # Claim this specific row directly rather than through WorkQueue's
    # batch claim: migrated_db is shared across every test in this file,
    # so a batch_size=1 claim could otherwise pick up an unrelated
    # leftover pending row ordered ahead of this one.
    work_queue = WorkQueue(migrated_db, MessageEmbedding.__table__)
    async with migrated_db.session() as session:
        item_id = (
            await session.execute(
                text(
                    "SELECT id FROM message_embeddings "
                    "WHERE model = :m AND account_id = :a"
                ),
                {"m": model, "a": account_id},
            )
        ).scalar_one()
        await session.execute(
            text(
                "UPDATE message_embeddings SET status = 'claimed', claimed_by = 'w1' "
                "WHERE id = :id"
            ),
            {"id": item_id},
        )
    claimed = [{"id": item_id, "account_id": account_id, "message_id": mail_id, "model": model}]

    class _FakeSettings:
        def get(self, category: str) -> dict[str, object]:
            return {"content_chars": 2000, "provider": "fake"}

        def has_category(self, category: str) -> bool:
            return False

    provider_calls: list[str] = []

    class _FakeProvider(FakeEmbeddingProvider):
        async def embed_batch(self, texts: list[str], *, model: str) -> list[list[float]]:
            provider_calls.append(texts[0])
            return await super().embed_batch(texts, model=model)

    import mail_verdict.embeddings.worker as worker_module

    original_resolve = worker_module.resolve_embedding_provider
    worker_module.resolve_embedding_provider = lambda *a, **k: _FakeProvider()  # type: ignore
    try:
        await _handle_one(
            claimed[0], "w1", work_queue, embedding_repo, message_repo,
            cred_repo=None, settings_service=_FakeSettings(),  # type: ignore[arg-type]
            circuit=_NullCircuit(),  # type: ignore[arg-type]
        )
    finally:
        worker_module.resolve_embedding_provider = original_resolve

    assert "should be ignored" not in provider_calls[0]

    async with migrated_db.session() as session:
        row = (
            await session.execute(
                text("SELECT status, content_level FROM message_embeddings WHERE id = :id"),
                {"id": item_id},
            )
        ).one()
    assert row.status == "done"
    assert row.content_level == "envelope"


class _NullCircuit:
    """A circuit breaker stand-in that never records anything -- the
    worker test above only needs record_success() to be awaitable."""

    async def record_success(self) -> None:
        return None

    async def record_unavailable(self, **kwargs: object) -> None:
        return None

    async def record_backoff(self, **kwargs: object) -> None:
        return None


@pytest.mark.asyncio
async def test_semantic_search_orders_nearest_first(migrated_db: DatabaseConnection) -> None:
    """Cosine distance ordering: a vector identical to the query ranks
    ahead of an orthogonal one."""
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        close_id = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, subject="close",
        )
        far_id = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, subject="far",
        )
        await session.commit()

    query_vector = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
    close_vector = [0.99] + [0.01] * (EMBEDDING_DIMENSIONS - 1)
    far_vector = [0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2)

    async with migrated_db.session() as session:
        for mail_id, vector in ((close_id, close_vector), (far_id, far_vector)):
            await session.execute(
                MessageEmbedding.__table__.insert().values(
                    account_id=account_id, msg_key=str(mail_id), message_id=mail_id,
                    model=model, status="done", embedding=vector,
                )
            )

    results = await semantic_search(
        migrated_db, query_vector=query_vector, model=model, account_id=account_id, k=10,
    )
    assert len(results) == 2
    assert results[0].message.id == close_id
    assert results[1].message.id == far_id
    assert results[0].similarity > results[1].similarity
