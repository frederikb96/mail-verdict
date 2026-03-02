"""Tests for EmbeddingWorker: queue management, lifecycle, batch processing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mail_verdict.semantic.worker import (
    EMBED_BATCH_SIZE,
    EmbedItem,
    EmbeddingWorker,
    get_embedding_worker,
    init_embedding_worker,
    reset_embedding_worker,
)


def _make_item(
    mail_id: str = "00000000-0000-0000-0000-000000000001",
    account_id: str = "acc-1",
) -> EmbedItem:
    """Create a test EmbedItem."""
    return EmbedItem(
        mail_id=mail_id,
        account_id=account_id,
        from_addr="alice@example.com",
        subject="Test",
        body_text="Hello world",
        is_spam=None,
        folder="INBOX",
        from_domain="example.com",
        received_at=None,
    )


class TestEmbeddingWorkerLifecycle:
    """Tests for start/stop and singleton."""

    @pytest.mark.asyncio
    async def test_start_sets_running(self) -> None:
        """start() marks the worker as running."""
        worker = EmbeddingWorker()
        await worker.start()
        assert worker._running is True
        await worker.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_state(self) -> None:
        """stop() clears running flag and workers/queues."""
        worker = EmbeddingWorker()
        await worker.start()
        await worker.stop()
        assert worker._running is False
        assert len(worker._workers) == 0
        assert len(worker._queues) == 0

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self) -> None:
        """stop() is safe when not started."""
        worker = EmbeddingWorker()
        await worker.stop()
        assert worker._running is False


class TestEnqueue:
    """Tests for enqueue method."""

    @pytest.mark.asyncio
    async def test_rejects_when_not_running(self) -> None:
        """enqueue returns False when worker is not running."""
        worker = EmbeddingWorker()
        item = _make_item()
        result = await worker.enqueue(item)
        assert result is False

    @pytest.mark.asyncio
    async def test_enqueue_creates_queue(self) -> None:
        """enqueue creates a queue for the account."""
        worker = EmbeddingWorker()
        await worker.start()
        item = _make_item()
        result = await worker.enqueue(item)
        assert result is True
        assert item.account_id in worker._queues
        await worker.stop()

    @pytest.mark.asyncio
    async def test_enqueue_batch_counts(self) -> None:
        """enqueue_batch returns number of successfully enqueued items."""
        worker = EmbeddingWorker()
        await worker.start()
        items = [_make_item(mail_id=f"id-{i}") for i in range(3)]
        count = await worker.enqueue_batch(items)
        assert count == 3
        await worker.stop()


class TestQueueSize:
    """Tests for get_queue_size."""

    def test_no_queue_returns_zero(self) -> None:
        """Queue size is 0 for unknown account."""
        worker = EmbeddingWorker()
        assert worker.get_queue_size("unknown") == 0


class TestSingleton:
    """Tests for module-level singleton."""

    def test_init_and_get(self) -> None:
        """init creates and get retrieves the singleton."""
        worker = init_embedding_worker(max_queue_size=100)
        assert get_embedding_worker() is worker
        reset_embedding_worker()

    def test_get_before_init_raises(self) -> None:
        """get_embedding_worker raises if not initialized."""
        reset_embedding_worker()
        with pytest.raises(RuntimeError, match="not initialized"):
            get_embedding_worker()

    def test_reset_clears(self) -> None:
        """reset_embedding_worker clears the singleton."""
        init_embedding_worker()
        reset_embedding_worker()
        with pytest.raises(RuntimeError):
            get_embedding_worker()
