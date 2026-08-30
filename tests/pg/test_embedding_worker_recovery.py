"""
The embedding worker's circuit-breaker wiring: the breaker QueueManager
reports for the "embeddings" queue is the same one _handle_one actually
writes to, a suspended breaker recovers once the worker loop wins a probe,
and a persistently retryable failure reaches a terminal state instead of
retrying forever.
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from datetime import datetime as dt
from datetime import timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import MessageEmbedding
from mail_verdict.database.repository import MessageRepository
from mail_verdict.embeddings.provider import FakeEmbeddingProvider
from mail_verdict.embeddings.repository import EmbeddingRepository
from mail_verdict.embeddings.worker import (
    CIRCUIT_NAME,
    QUEUE_NAME,
    _handle_one,
    _run_worker,
    register_embeddings,
)
from mail_verdict.queue.circuit import CircuitBreaker, CircuitState
from mail_verdict.queue.manager import QueueManager
from mail_verdict.queue.work_queue import WorkQueue
from mail_verdict.settings.service import SettingsService

_imap_uid_counter = itertools.count(1)


def _unique_model() -> str:
    """migrated_db is shared across the file's tests -- see
    tests/pg/test_message_embeddings.py's identical helper for why."""
    return f"model-{uuid.uuid4().hex[:8]}"


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
        text(
            "INSERT INTO folders (id, account_id, imap_name, initial_sync_done) "
            "VALUES (:id, :account_id, 'INBOX', true)"
        ),
        {"id": folder_id, "account_id": account_id},
    )
    return account_id, folder_id


async def _seed_message(
    session: AsyncSession, *, account_id: uuid.UUID, folder_id: uuid.UUID,
) -> uuid.UUID:
    mail_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, from_addr, "
            "subject, body_text, received_at, size_bytes) "
            "VALUES (:id, :account_id, :folder_id, :imap_uid, :thread_id, :message_id, "
            "'sender@example.com', 'Hello', 'Body.', :received_at, 1024)"
        ),
        {
            "id": mail_id, "account_id": account_id, "folder_id": folder_id,
            "imap_uid": next(_imap_uid_counter), "thread_id": uuid.uuid4(),
            "message_id": f"<{uuid.uuid4()}@example.com>",
            "received_at": dt(2026, 1, 1, tzinfo=timezone.utc),
        },
    )
    return mail_id


async def _settings(db: DatabaseConnection) -> SettingsService:
    service = SettingsService(db)
    await service.load()
    return service


class _FakeSettings:
    """A minimal settings_service stand-in -- .load() is never needed
    since every value is fixed rather than read from the database."""

    def __init__(self, **overrides: object) -> None:
        self._values: dict[str, object] = {
            "content_chars": 2000, "provider": "fake", "batch_size": 10,
            "max_attempts": 2, "base_delay_seconds": 0.0, "max_delay_seconds": 0.0,
        }
        self._values.update(overrides)

    def get(self, category: str) -> dict[str, object]:
        return self._values

    def has_category(self, category: str) -> bool:
        return False


async def _claim_specific(
    db: DatabaseConnection, item_id: uuid.UUID, *, worker_id: str, lease_seconds: float = 30,
) -> dict[str, object]:
    """Claim exactly one row by id, mirroring WorkQueue.claim_batch's own
    UPDATE -- used instead of a batch claim so a test proving repeated
    claim/retry cycles for one row is never at the mercy of migrated_db's
    other, unrelated pending rows sorting ahead of it."""
    async with db.session() as session:
        result = await session.execute(
            text(
                "UPDATE message_embeddings SET status = 'claimed', claimed_by = :worker_id, "
                "claimed_at = now(), "
                "lease_expires_at = now() + make_interval(secs => :lease_seconds), "
                "attempts = attempts + 1 "
                "WHERE id = :id AND status = 'pending' "
                "RETURNING *"
            ),
            {"id": item_id, "worker_id": worker_id, "lease_seconds": lease_seconds},
        )
        return dict(result.mappings().one())


class _AlwaysConnectionError(FakeEmbeddingProvider):
    """Raises an exception named like openai's APIConnectionError on every
    call -- _is_retryable matches by class name alone (see
    embeddings/worker.py), so this need not import the real SDK type."""

    async def embed_batch(self, texts: list[str], *, model: str) -> list[list[float]]:
        raise APIConnectionError("connection dropped")


class APIConnectionError(Exception):
    pass


@pytest.mark.asyncio
async def test_registered_circuit_name_matches_the_one_the_worker_writes(
    migrated_db: DatabaseConnection,
) -> None:
    """QueueManager.register's circuit_name must be the provider name
    _handle_one's CircuitBreaker actually writes to -- otherwise the
    observability surface reports a breaker nobody ever trips while the
    real one goes unseen."""
    queue_manager = QueueManager(migrated_db)
    settings_service = await _settings(migrated_db)

    register_embeddings(
        queue_manager, migrated_db, cred_repo=None,  # type: ignore[arg-type]
        settings_service=settings_service,
    )

    summary = await queue_manager.summary(QUEUE_NAME)
    assert summary.circuit.name == CIRCUIT_NAME


@pytest.mark.asyncio
async def test_suspended_worker_loop_recovers_once_it_wins_a_probe(
    migrated_db: DatabaseConnection,
) -> None:
    """The fresh-install reproduction: a breaker suspended (no key
    configured) that nothing ever probes stays suspended forever, even
    after the key is fixed. The worker loop must call try_probe itself."""
    circuit_name = f"provider-{uuid.uuid4().hex[:8]}"
    circuit = CircuitBreaker(migrated_db, circuit_name)
    await circuit.record_unavailable(
        reason="no key configured", probe_interval=timedelta(seconds=0),
    )
    assert await circuit.is_available() is False

    model = _unique_model()
    embedding_repo = EmbeddingRepository(migrated_db)
    message_repo = MessageRepository(migrated_db)
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        await _seed_message(session, account_id=account_id, folder_id=folder_id)
        await session.commit()
    await embedding_repo.enqueue_missing_batch(model=model, batch_size=50, account_id=account_id)

    async def _unused_worker_body(worker_id: str, stop_event: asyncio.Event) -> None:
        """Never actually run -- _run_worker is driven directly below,
        never through the supervisor this would be registered for."""
        return None

    queue_manager = QueueManager(migrated_db)
    queue_manager.register(
        QUEUE_NAME, MessageEmbedding.__table__, _unused_worker_body, circuit_name=CIRCUIT_NAME,
    )

    import mail_verdict.embeddings.worker as worker_module

    original_resolve = worker_module.resolve_embedding_provider
    worker_module.resolve_embedding_provider = lambda *a, **k: FakeEmbeddingProvider()  # type: ignore[assignment]

    stop_event = asyncio.Event()
    loop_task = asyncio.create_task(
        _run_worker(
            queue_manager, "recovery-worker", stop_event, embedding_repo, message_repo,
            cred_repo=None, settings_service=_FakeSettings(), circuit=circuit,  # type: ignore[arg-type]
        )
    )
    try:
        async def _done() -> bool:
            async with migrated_db.session() as session:
                status = (
                    await session.execute(
                        text(
                            "SELECT status FROM message_embeddings "
                            "WHERE account_id = :a AND model = :m"
                        ),
                        {"a": account_id, "m": model},
                    )
                ).scalar_one()
            return status == "done"

        deadline = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < deadline:
            if await _done():
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("embedding never reached 'done' -- the probe never recovered it")
    finally:
        stop_event.set()
        await asyncio.wait_for(loop_task, timeout=5.0)
        worker_module.resolve_embedding_provider = original_resolve

    status = await circuit.status()
    assert status.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_a_persistently_retryable_failure_reaches_a_dead_letter(
    migrated_db: DatabaseConnection,
) -> None:
    """A connection-drop-shaped failure that recurs for one payload must
    reach 'failed' once it exceeds max_attempts, unlike a shared-resource
    throttle (RateLimitError), which is never capped."""
    embedding_repo = EmbeddingRepository(migrated_db)
    message_repo = MessageRepository(migrated_db)
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        await _seed_message(session, account_id=account_id, folder_id=folder_id)
        await session.commit()
    await embedding_repo.enqueue_missing_batch(model=model, batch_size=50, account_id=account_id)

    async with migrated_db.session() as session:
        item_id = (
            await session.execute(
                text(
                    "SELECT id FROM message_embeddings WHERE model = :m AND account_id = :a"
                ),
                {"m": model, "a": account_id},
            )
        ).scalar_one()

    work_queue = WorkQueue(migrated_db, MessageEmbedding.__table__)
    circuit = CircuitBreaker(migrated_db, f"provider-{uuid.uuid4().hex[:8]}")
    settings_service = _FakeSettings(max_attempts=2)

    import mail_verdict.embeddings.worker as worker_module

    original_resolve = worker_module.resolve_embedding_provider
    worker_module.resolve_embedding_provider = lambda *a, **k: _AlwaysConnectionError()  # type: ignore[assignment]
    try:
        # max_attempts=2: the first claim (attempts=1) is still under the
        # cap and requeues; the second (attempts=2) reaches it and fails.
        first = await _claim_specific(migrated_db, item_id, worker_id="w1")
        assert first["attempts"] == 1
        await _handle_one(
            first, "w1", work_queue, embedding_repo, message_repo,
            cred_repo=None, settings_service=settings_service, circuit=circuit,  # type: ignore[arg-type]
        )

        async with migrated_db.session() as session:
            mid = (
                await session.execute(
                    text("SELECT status, attempts FROM message_embeddings WHERE id = :id"),
                    {"id": item_id},
                )
            ).one()
        assert mid.status == "pending"  # requeued, not yet at the cap
        assert mid.attempts == 1

        second = await _claim_specific(migrated_db, item_id, worker_id="w1")
        assert second["attempts"] == 2
        await _handle_one(
            second, "w1", work_queue, embedding_repo, message_repo,
            cred_repo=None, settings_service=settings_service, circuit=circuit,  # type: ignore[arg-type]
        )
    finally:
        worker_module.resolve_embedding_provider = original_resolve

    async with migrated_db.session() as session:
        final = (
            await session.execute(
                text("SELECT status FROM message_embeddings WHERE id = :id"), {"id": item_id},
            )
        ).one()
    assert final.status == "failed"
