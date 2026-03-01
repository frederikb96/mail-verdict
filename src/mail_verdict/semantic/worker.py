"""
Embedding Worker for MailVerdict.

Async queue-based worker that processes new mails for embedding.
Follows the Engram QueueManager pattern: bounded queue per account,
sequential FIFO processing, idle timeout, batching.

Integration: receives mail data, calls SemanticStore for embedding+storage.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from qdrant_client.http import models as qdrant_models

from mail_verdict.semantic.store import SemanticStore, get_semantic_store

logger = logging.getLogger(__name__)

# Max mails per OpenAI embedding batch call
EMBED_BATCH_SIZE = 64


@dataclass
class EmbedItem:
    """Work unit for the embedding queue."""

    mail_id: str
    account_id: str
    from_addr: str | None
    subject: str | None
    body_text: str | None
    is_spam: bool | None
    folder: str | None
    from_domain: str | None
    received_at: Any  # datetime | None
    excerpt_length: int = 500


class EmbeddingWorker:
    """
    Async queue-based embedding worker.

    Manages bounded queues per account with one worker task each.
    Workers process items FIFO, batching OpenAI calls where possible.
    Idle workers exit after timeout; new items respawn them.
    """

    def __init__(
        self,
        *,
        max_queue_size: int = 1000,
        idle_timeout_seconds: float = 300.0,
    ) -> None:
        """
        Initialize the embedding worker manager.

        Args:
            max_queue_size: Max items per account queue before backpressure
            idle_timeout_seconds: Seconds of idle before a worker exits
        """
        self._max_queue_size = max_queue_size
        self._idle_timeout = idle_timeout_seconds
        self._queues: dict[str, asyncio.Queue[EmbedItem]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._running = False

    async def start(self) -> None:
        """Mark worker manager as running."""
        self._running = True
        logger.info("Embedding worker manager started")

    async def stop(self) -> None:
        """Stop all account workers gracefully."""
        if not self._running:
            return

        self._running = False
        logger.info("Stopping %d embedding workers", len(self._workers))

        for task in self._workers.values():
            task.cancel()

        for task in self._workers.values():
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._workers.clear()
        self._queues.clear()
        logger.info("Embedding worker manager stopped")

    async def enqueue(self, item: EmbedItem) -> bool:
        """
        Enqueue a mail for embedding.

        Blocks if the account's queue is full (backpressure).
        Starts a worker for the account if not already running.

        Args:
            item: The embedding work item

        Returns:
            True if enqueued, False if manager not running
        """
        if not self._running:
            logger.warning("Embedding worker not running, rejecting item")
            return False

        account_id = item.account_id

        if account_id not in self._queues:
            self._queues[account_id] = asyncio.Queue(maxsize=self._max_queue_size)

        await self._queues[account_id].put(item)

        # Spawn worker if needed
        if account_id not in self._workers or self._workers[account_id].done():
            self._workers[account_id] = asyncio.create_task(
                self._worker_loop(account_id),
                name=f"embed-worker-{account_id[:8]}",
            )

        return True

    async def enqueue_batch(self, items: list[EmbedItem]) -> int:
        """
        Enqueue multiple items.

        Args:
            items: List of embedding work items

        Returns:
            Number of items successfully enqueued
        """
        count = 0
        for item in items:
            if await self.enqueue(item):
                count += 1
        return count

    async def _worker_loop(self, account_id: str) -> None:
        """
        Process items from an account's queue with batching.

        Collects items up to EMBED_BATCH_SIZE for batched OpenAI calls.
        Exits after idle timeout with no items.

        Args:
            account_id: Account whose queue to process
        """
        queue = self._queues.get(account_id)
        if not queue:
            return

        logger.info("Embedding worker started for account %s", account_id[:8])

        while self._running:
            try:
                # Wait for first item with idle timeout
                try:
                    first_item = await asyncio.wait_for(queue.get(), timeout=self._idle_timeout)
                except asyncio.TimeoutError:
                    logger.info(
                        "Embedding worker for account %s idle timeout, stopping",
                        account_id[:8],
                    )
                    break

                # Collect batch: first item + whatever else is immediately available
                batch = [first_item]
                while len(batch) < EMBED_BATCH_SIZE:
                    try:
                        batch.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                await self._process_batch(batch)

            except asyncio.CancelledError:
                logger.info("Embedding worker for account %s cancelled", account_id[:8])
                break
            except Exception:
                logger.exception("Embedding worker error for account %s", account_id[:8])

        self._workers.pop(account_id, None)

    async def _process_batch(self, batch: list[EmbedItem]) -> None:
        """
        Process a batch of embed items with a single OpenAI call.

        Args:
            batch: List of EmbedItem to embed and store
        """
        store = get_semantic_store()

        # Build embedding texts
        texts: list[str] = []
        valid_items: list[EmbedItem] = []
        for item in batch:
            text = SemanticStore.build_embedding_text(
                from_addr=item.from_addr,
                subject=item.subject,
                body_text=item.body_text,
                excerpt_length=item.excerpt_length,
            )
            if text.strip():
                texts.append(text)
                valid_items.append(item)

        if not texts:
            logger.debug("Batch of %d items produced no embeddable text", len(batch))
            return

        # Batch embed
        vectors = await store.embed(texts)
        if vectors is None:
            logger.warning("Batch embedding failed for %d items", len(texts))
            return

        # Upsert each item with its pre-computed vector
        stored = 0
        for item, text, vector in zip(valid_items, texts, vectors):
            try:
                content_hash = SemanticStore._content_hash(text)
                payload = SemanticStore._build_payload(
                    mail_id=item.mail_id,
                    account_id=item.account_id,
                    content_hash=content_hash,
                    is_spam=item.is_spam,
                    folder=item.folder,
                    from_domain=item.from_domain,
                    received_at=item.received_at,
                )

                await store._qdrant.upsert(
                    collection_name=store._collection,
                    points=[
                        qdrant_models.PointStruct(
                            id=item.mail_id,
                            vector=vector,
                            payload=payload,
                        )
                    ],
                )
                stored += 1
            except Exception:
                logger.warning(
                    "Failed to store embedding for mail %s",
                    item.mail_id[:8],
                    exc_info=True,
                )

        logger.info(
            "Batch embedded %d/%d mails for account %s",
            stored,
            len(valid_items),
            valid_items[0].account_id[:8] if valid_items else "?",
        )

    def get_queue_size(self, account_id: str) -> int:
        """
        Get current queue size for an account.

        Args:
            account_id: Account UUID string

        Returns:
            Number of items in queue, 0 if no queue exists
        """
        queue = self._queues.get(account_id)
        return queue.qsize() if queue else 0


# Module-level singleton
_worker_instance: EmbeddingWorker | None = None


def init_embedding_worker(
    *,
    max_queue_size: int = 1000,
    idle_timeout_seconds: float = 300.0,
) -> EmbeddingWorker:
    """
    Create the global EmbeddingWorker instance.

    Args:
        max_queue_size: Max items per account queue
        idle_timeout_seconds: Worker idle timeout

    Returns:
        The initialized EmbeddingWorker
    """
    global _worker_instance
    _worker_instance = EmbeddingWorker(
        max_queue_size=max_queue_size,
        idle_timeout_seconds=idle_timeout_seconds,
    )
    return _worker_instance


def get_embedding_worker() -> EmbeddingWorker:
    """
    Get the global EmbeddingWorker singleton.

    Raises:
        RuntimeError: If worker not initialized
    """
    if _worker_instance is None:
        raise RuntimeError("EmbeddingWorker not initialized. Call init_embedding_worker() first.")
    return _worker_instance


def reset_embedding_worker() -> None:
    """Reset the global worker instance (for testing and shutdown)."""
    global _worker_instance
    _worker_instance = None
