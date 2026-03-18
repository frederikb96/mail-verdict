"""
Semantic Store for MailVerdict.

Vector store backed by Qdrant and OpenAI embeddings for mail similarity search.
Used by spam detection to find similar previously-classified mails.

Key features:
- Async Qdrant client (shared from server lifespan)
- OpenAI embeddings (model configured via config.yaml)
- Smart upsert: skip re-embedding if content unchanged
- Metadata filtering by account_id, is_spam, from_domain, date range
- Graceful degradation: return empty results on Qdrant/OpenAI failure
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from openai import AsyncOpenAI
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qdrant_models
from qdrant_client.http.exceptions import UnexpectedResponse

from mail_verdict.config import AIConfig, QdrantConfig

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """A single similarity search result."""

    point_id: str
    mail_id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


class SemanticStore:
    """
    Async semantic search store for mail embeddings.

    Wraps Qdrant for vector storage and OpenAI for embedding generation.
    Designed as a singleton initialized during server lifespan.

    Args:
        qdrant_client: Shared async Qdrant client from server lifespan
        qdrant_config: Qdrant configuration (collection name, etc.)
        ai_config: AI configuration (embedding model, dimensions)
    """

    _instance: SemanticStore | None = None

    def __init__(
        self,
        qdrant_client: AsyncQdrantClient,
        qdrant_config: QdrantConfig,
        ai_config: AIConfig,
        openai_client: AsyncOpenAI | None = None,
    ) -> None:
        """
        Initialize the semantic store.

        Args:
            qdrant_client: Async Qdrant client (managed externally)
            qdrant_config: Collection name and connection settings
            ai_config: Embedding model and dimensions
            openai_client: Shared AsyncOpenAI client (creates one if not provided)
        """
        self._qdrant = qdrant_client
        self._collection = qdrant_config.collection_name
        self._embedding_model = ai_config.embedding_model
        self._embedding_dimensions = ai_config.embedding_dimensions
        self._openai: AsyncOpenAI | None = openai_client
        self._collection_ready = False

    @classmethod
    def init_instance(
        cls,
        qdrant_client: AsyncQdrantClient,
        qdrant_config: QdrantConfig,
        ai_config: AIConfig,
        openai_client: AsyncOpenAI | None = None,
    ) -> SemanticStore:
        """
        Create and set the singleton instance.

        Args:
            qdrant_client: Async Qdrant client
            qdrant_config: Qdrant configuration
            ai_config: AI configuration
            openai_client: Shared AsyncOpenAI client

        Returns:
            The initialized SemanticStore singleton
        """
        cls._instance = SemanticStore(
            qdrant_client,
            qdrant_config,
            ai_config,
            openai_client,
        )
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton (for testing and shutdown)."""
        cls._instance = None

    async def ensure_collection(self) -> bool:
        """
        Ensure the Qdrant collection exists with proper vector config and indexes.

        Returns:
            True if collection is ready, False on failure
        """
        if self._collection_ready:
            return True

        try:
            collections = await self._qdrant.get_collections()
            exists = any(c.name == self._collection for c in collections.collections)

            if not exists:
                logger.info("Creating Qdrant collection '%s'", self._collection)
                await self._qdrant.create_collection(
                    collection_name=self._collection,
                    vectors_config=qdrant_models.VectorParams(
                        size=self._embedding_dimensions,
                        distance=qdrant_models.Distance.COSINE,
                    ),
                )

                # Payload indexes for filtered searches
                for field_name in ("account_id", "is_spam", "from_domain"):
                    await self._qdrant.create_payload_index(
                        collection_name=self._collection,
                        field_name=field_name,
                        field_schema=qdrant_models.PayloadSchemaType.KEYWORD,
                    )

                await self._qdrant.create_payload_index(
                    collection_name=self._collection,
                    field_name="received_at",
                    field_schema=qdrant_models.PayloadSchemaType.DATETIME,
                )

                logger.info("Collection '%s' created with payload indexes", self._collection)

            self._collection_ready = True
            return True

        except Exception as e:
            logger.warning("Failed to ensure Qdrant collection: %s", e)
            return False

    def _get_openai(self) -> AsyncOpenAI:
        """Get or create the async OpenAI client."""
        if self._openai is None:
            self._openai = AsyncOpenAI()
        return self._openai

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """
        Generate embeddings for a batch of texts via OpenAI.

        Args:
            texts: List of text strings to embed

        Returns:
            List of embedding vectors, or None on failure
        """
        if not texts:
            return []

        try:
            client = self._get_openai()
            response = await client.embeddings.create(
                input=texts,
                model=self._embedding_model,
            )
            return [item.embedding for item in response.data]

        except Exception as e:
            logger.warning("Embedding generation failed: %s", e)
            return None

    @staticmethod
    def _content_hash(text: str) -> str:
        """Generate a stable hash for content change detection."""
        return hashlib.sha256(text.encode()).hexdigest()

    @staticmethod
    def build_embedding_text(
        from_addr: str | None,
        subject: str | None,
        body_text: str | None,
        excerpt_length: int = 500,
    ) -> str:
        """
        Build the text to embed from mail fields.

        Args:
            from_addr: Sender address
            subject: Mail subject
            body_text: Plain text body
            excerpt_length: Max chars of body to include

        Returns:
            Combined text for embedding
        """
        parts: list[str] = []
        if from_addr:
            parts.append(f"From: {from_addr}")
        if subject:
            parts.append(f"Subject: {subject}")
        if body_text:
            parts.append(body_text[:excerpt_length])
        return "\n".join(parts)

    async def upsert(
        self,
        mail_id: str,
        text: str,
        *,
        account_id: str,
        is_spam: bool | None = None,
        folder: str | None = None,
        from_domain: str | None = None,
        received_at: datetime | None = None,
    ) -> bool:
        """
        Embed and store a mail in the vector index.

        Smart upsert: skips re-embedding if content is unchanged (hash match).

        Args:
            mail_id: UUID of the mail (used as Qdrant point ID)
            text: Pre-built embedding text (from build_embedding_text)
            account_id: Owning account UUID string
            is_spam: Spam label (True/False/None)
            folder: IMAP folder name
            from_domain: Sender domain for filtering
            received_at: Mail receive timestamp

        Returns:
            True if stored successfully, False on failure
        """
        if not text or not text.strip():
            logger.debug("Empty embedding text for mail %s, skipping", mail_id)
            return False

        if not await self.ensure_collection():
            return False

        content_hash = self._content_hash(text)

        # Check if point exists with same content hash (skip re-embedding)
        try:
            existing = await self._qdrant.retrieve(
                collection_name=self._collection,
                ids=[mail_id],
                with_payload=True,
            )
            if existing and existing[0].payload:
                existing_hash = existing[0].payload.get("content_hash")
                if existing_hash == content_hash:
                    # Content unchanged - update payload only (no re-embedding)
                    payload = self._build_payload(
                        mail_id,
                        account_id,
                        content_hash,
                        is_spam=is_spam,
                        folder=folder,
                        from_domain=from_domain,
                        received_at=received_at,
                    )
                    await self._qdrant.set_payload(
                        collection_name=self._collection,
                        payload=payload,
                        points=[mail_id],
                    )
                    logger.debug("Payload updated (no re-embed) for mail %s", mail_id[:8])
                    return True
        except UnexpectedResponse:
            pass  # Point doesn't exist yet
        except Exception as e:
            logger.debug("Smart upsert check failed for %s: %s", mail_id[:8], e)

        # Generate embedding
        vectors = await self.embed([text])
        if vectors is None or not vectors:
            return False

        payload = self._build_payload(
            mail_id,
            account_id,
            content_hash,
            is_spam=is_spam,
            folder=folder,
            from_domain=from_domain,
            received_at=received_at,
        )

        try:
            await self._qdrant.upsert(
                collection_name=self._collection,
                points=[
                    qdrant_models.PointStruct(
                        id=mail_id,
                        vector=vectors[0],
                        payload=payload,
                    )
                ],
            )
            logger.debug("Stored embedding for mail %s", mail_id[:8])
            return True

        except Exception as e:
            logger.warning("Failed to upsert mail %s to Qdrant: %s", mail_id[:8], e)
            return False

    @staticmethod
    def _build_payload(
        mail_id: str,
        account_id: str,
        content_hash: str,
        *,
        is_spam: bool | None = None,
        folder: str | None = None,
        from_domain: str | None = None,
        received_at: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Build the Qdrant payload dict.

        Args:
            mail_id: Mail UUID string
            account_id: Account UUID string
            content_hash: SHA256 of embedding text
            is_spam: Spam classification
            folder: IMAP folder name
            from_domain: Sender domain
            received_at: Mail receive timestamp

        Returns:
            Payload dict for Qdrant point
        """
        payload: dict[str, Any] = {
            "mail_id": mail_id,
            "account_id": account_id,
            "content_hash": content_hash,
        }
        if is_spam is not None:
            payload["is_spam"] = str(is_spam).lower()
        if folder is not None:
            payload["folder"] = folder
        if from_domain is not None:
            payload["from_domain"] = from_domain
        if received_at is not None:
            payload["received_at"] = received_at.isoformat()
        return payload

    async def update_payload(self, mail_id: str, payload: dict[str, Any]) -> bool:
        """
        Update Qdrant point payload without re-embedding.

        Args:
            mail_id: Mail UUID string (Qdrant point ID)
            payload: Payload fields to set/update

        Returns:
            True if update succeeded, False on failure
        """
        if not await self.ensure_collection():
            return False

        try:
            await self._qdrant.set_payload(
                collection_name=self._collection,
                payload=payload,
                points=[mail_id],
            )
            return True
        except Exception as e:
            logger.warning("Payload update failed for %s: %s", mail_id[:8], e)
            return False

    async def search(
        self,
        query_text: str,
        *,
        limit: int = 5,
        score_threshold: float = 0.0,
        account_id: str | None = None,
        is_spam: bool | None = None,
        from_domain: str | None = None,
        received_after: datetime | None = None,
        received_before: datetime | None = None,
        exclude_ids: list[str] | None = None,
    ) -> list[SearchResult]:
        """
        Find similar mails by semantic similarity.

        Args:
            query_text: Text to search for (will be embedded)
            limit: Max number of results
            score_threshold: Minimum cosine similarity score
            account_id: Filter by account
            is_spam: Filter by spam label
            from_domain: Filter by sender domain
            received_after: Filter mails received after this timestamp
            received_before: Filter mails received before this timestamp
            exclude_ids: Point IDs to exclude from results

        Returns:
            List of SearchResult ordered by descending similarity
        """
        if not await self.ensure_collection():
            return []

        vectors = await self.embed([query_text])
        if vectors is None or not vectors:
            return []

        qdrant_filter = self._build_search_filter(
            account_id=account_id,
            is_spam=is_spam,
            from_domain=from_domain,
            received_after=received_after,
            received_before=received_before,
            exclude_ids=exclude_ids,
        )

        try:
            response = await self._qdrant.query_points(
                collection_name=self._collection,
                query=vectors[0],
                limit=limit,
                score_threshold=score_threshold,
                query_filter=qdrant_filter,
            )

            results: list[SearchResult] = []
            for hit in response.points:
                payload = dict(hit.payload) if hit.payload else {}
                mail_id = payload.pop("mail_id", str(hit.id))
                results.append(
                    SearchResult(
                        point_id=str(hit.id),
                        mail_id=mail_id,
                        score=hit.score if hit.score is not None else 0.0,
                        payload=payload,
                    )
                )

            logger.debug(
                "Similarity search returned %d results (threshold=%.2f)",
                len(results),
                score_threshold,
            )
            return results

        except Exception as e:
            logger.warning("Similarity search failed: %s", e)
            return []

    @staticmethod
    def _build_search_filter(
        *,
        account_id: str | None = None,
        is_spam: bool | None = None,
        from_domain: str | None = None,
        received_after: datetime | None = None,
        received_before: datetime | None = None,
        exclude_ids: list[str] | None = None,
    ) -> qdrant_models.Filter | None:
        """
        Build a Qdrant filter from search parameters.

        Args:
            account_id: Filter by account
            is_spam: Filter by spam label
            from_domain: Filter by sender domain
            received_after: Lower bound on received_at
            received_before: Upper bound on received_at
            exclude_ids: Point IDs to exclude

        Returns:
            Qdrant Filter or None if no filters specified
        """
        must: list[qdrant_models.Condition] = []
        must_not: list[qdrant_models.Condition] = []

        if account_id is not None:
            must.append(
                qdrant_models.FieldCondition(
                    key="account_id",
                    match=qdrant_models.MatchValue(value=account_id),
                )
            )

        if is_spam is not None:
            must.append(
                qdrant_models.FieldCondition(
                    key="is_spam",
                    match=qdrant_models.MatchValue(value=str(is_spam).lower()),
                )
            )

        if from_domain is not None:
            must.append(
                qdrant_models.FieldCondition(
                    key="from_domain",
                    match=qdrant_models.MatchValue(value=from_domain),
                )
            )

        if received_after is not None or received_before is not None:
            must.append(
                qdrant_models.FieldCondition(
                    key="received_at",
                    range=qdrant_models.DatetimeRange(
                        gte=received_after,
                        lte=received_before,
                    ),
                )
            )

        if exclude_ids:
            must_not.append(
                qdrant_models.HasIdCondition(
                    has_id=list(exclude_ids),  # type: ignore[arg-type]
                )
            )

        if not must and not must_not:
            return None

        return qdrant_models.Filter(
            must=must or None,
            must_not=must_not or None,
        )


def get_semantic_store() -> SemanticStore:
    """
    Get the global SemanticStore singleton.

    Raises:
        RuntimeError: If store not initialized via init_instance()
    """
    if SemanticStore._instance is None:
        raise RuntimeError("SemanticStore not initialized. Call init_instance() first.")
    return SemanticStore._instance
