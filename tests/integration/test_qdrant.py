"""
Integration tests: Qdrant vector store operations.

Requires podman-compose.test.yaml Qdrant container running on port 6334.

Markers: @pytest.mark.integration
"""

from __future__ import annotations

import uuid

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qdrant_models

from mail_verdict.config import AIConfig, QdrantConfig
from mail_verdict.semantic.store import SemanticStore

QDRANT_HOST = "localhost"
QDRANT_PORT = 6334
TEST_COLLECTION = f"test_mails_{uuid.uuid4().hex[:8]}"

# Dimensions for a mock embedding (not using real OpenAI in integration tests)
MOCK_DIMENSIONS = 128

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="module"),
]


def _qdrant_config() -> QdrantConfig:
    """Qdrant config pointing to test container."""
    return QdrantConfig(
        host=QDRANT_HOST,
        port=QDRANT_PORT,
        collection_name=TEST_COLLECTION,
    )


def _ai_config() -> AIConfig:
    """AI config with mock dimensions (we won't call real OpenAI)."""
    return AIConfig(
        provider="openai",
        model="gpt-4o-mini",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=MOCK_DIMENSIONS,
    )


@pytest.fixture(scope="module")
async def qdrant_client() -> AsyncQdrantClient:
    """Create a shared Qdrant client for the module."""
    client = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    yield client
    # Cleanup: delete test collection
    try:
        await client.delete_collection(TEST_COLLECTION)
    except Exception:
        pass
    await client.close()


@pytest.fixture(scope="module")
async def semantic_store(qdrant_client: AsyncQdrantClient) -> SemanticStore:
    """Create a SemanticStore backed by real Qdrant (but mock embeddings)."""
    store = SemanticStore(qdrant_client, _qdrant_config(), _ai_config())
    return store


def _random_vector(dims: int = MOCK_DIMENSIONS) -> list[float]:
    """Generate a random-ish unit vector for testing."""
    import hashlib

    seed = uuid.uuid4().hex
    values: list[float] = []
    for i in range(dims):
        h = hashlib.md5(f"{seed}-{i}".encode()).hexdigest()
        values.append((int(h[:8], 16) / 0xFFFFFFFF) * 2 - 1)
    # Normalize
    magnitude = sum(v**2 for v in values) ** 0.5
    if magnitude > 0:
        values = [v / magnitude for v in values]
    return values


class TestCollectionManagement:
    """Test Qdrant collection creation and management."""

    async def test_ensure_collection_creates(
        self, semantic_store: SemanticStore, qdrant_client: AsyncQdrantClient
    ) -> None:
        """Verify ensure_collection creates collection with correct config."""
        ok = await semantic_store.ensure_collection()
        assert ok is True

        # Verify collection exists
        collections = await qdrant_client.get_collections()
        names = [c.name for c in collections.collections]
        assert TEST_COLLECTION in names

    async def test_ensure_collection_idempotent(self, semantic_store: SemanticStore) -> None:
        """Verify calling ensure_collection twice is safe."""
        ok1 = await semantic_store.ensure_collection()
        ok2 = await semantic_store.ensure_collection()
        assert ok1 is True
        assert ok2 is True


class TestEmbedAndStore:
    """Test storing vectors with payloads in Qdrant."""

    async def test_upsert_point_directly(
        self, qdrant_client: AsyncQdrantClient, semantic_store: SemanticStore
    ) -> None:
        """Store a point directly with a known vector and verify retrieval."""
        await semantic_store.ensure_collection()

        point_id = str(uuid.uuid4())
        vector = _random_vector()
        account_id = str(uuid.uuid4())

        await qdrant_client.upsert(
            collection_name=TEST_COLLECTION,
            points=[
                qdrant_models.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "mail_id": point_id,
                        "account_id": account_id,
                        "is_spam": "false",
                        "from_domain": "example.com",
                        "content_hash": "abc123",
                    },
                )
            ],
        )

        # Retrieve and verify
        results = await qdrant_client.retrieve(
            collection_name=TEST_COLLECTION,
            ids=[point_id],
            with_payload=True,
            with_vectors=True,
        )
        assert len(results) == 1
        assert results[0].payload is not None
        assert results[0].payload["mail_id"] == point_id
        assert results[0].payload["account_id"] == account_id
        assert results[0].payload["is_spam"] == "false"

    async def test_upsert_multiple_points(
        self, qdrant_client: AsyncQdrantClient, semantic_store: SemanticStore
    ) -> None:
        """Store multiple points and verify count."""
        await semantic_store.ensure_collection()

        account_id = str(uuid.uuid4())
        points = []
        for i in range(5):
            pid = str(uuid.uuid4())
            points.append(
                qdrant_models.PointStruct(
                    id=pid,
                    vector=_random_vector(),
                    payload={
                        "mail_id": pid,
                        "account_id": account_id,
                        "is_spam": "true" if i % 2 == 0 else "false",
                        "from_domain": f"domain{i}.com",
                    },
                )
            )

        await qdrant_client.upsert(
            collection_name=TEST_COLLECTION,
            points=points,
        )

        # Scroll and count
        scroll_result = await qdrant_client.scroll(
            collection_name=TEST_COLLECTION,
            scroll_filter=qdrant_models.Filter(
                must=[
                    qdrant_models.FieldCondition(
                        key="account_id",
                        match=qdrant_models.MatchValue(value=account_id),
                    )
                ]
            ),
            limit=100,
        )
        assert len(scroll_result[0]) == 5


class TestSimilaritySearch:
    """Test vector similarity search with filters."""

    async def test_search_returns_similar_points(
        self, qdrant_client: AsyncQdrantClient, semantic_store: SemanticStore
    ) -> None:
        """Insert known vectors and search for the most similar."""
        await semantic_store.ensure_collection()

        account_id = str(uuid.uuid4())
        # Create a base vector and a slightly different one
        base = _random_vector()
        # Make a "similar" vector by adding small noise
        similar = [v + 0.01 for v in base]
        magnitude = sum(v**2 for v in similar) ** 0.5
        similar = [v / magnitude for v in similar]

        dissimilar = _random_vector()  # Completely different

        id_similar = str(uuid.uuid4())
        id_dissimilar = str(uuid.uuid4())

        await qdrant_client.upsert(
            collection_name=TEST_COLLECTION,
            points=[
                qdrant_models.PointStruct(
                    id=id_similar,
                    vector=similar,
                    payload={"mail_id": id_similar, "account_id": account_id},
                ),
                qdrant_models.PointStruct(
                    id=id_dissimilar,
                    vector=dissimilar,
                    payload={"mail_id": id_dissimilar, "account_id": account_id},
                ),
            ],
        )

        # Search using the base vector
        results = await qdrant_client.query_points(
            collection_name=TEST_COLLECTION,
            query=base,
            query_filter=qdrant_models.Filter(
                must=[
                    qdrant_models.FieldCondition(
                        key="account_id",
                        match=qdrant_models.MatchValue(value=account_id),
                    )
                ]
            ),
            limit=2,
        )

        assert len(results.points) == 2
        # The similar vector should rank first
        assert str(results.points[0].id) == id_similar

    async def test_search_with_spam_filter(
        self, qdrant_client: AsyncQdrantClient, semantic_store: SemanticStore
    ) -> None:
        """Search with is_spam metadata filter."""
        await semantic_store.ensure_collection()

        account_id = str(uuid.uuid4())
        vector = _random_vector()

        spam_id = str(uuid.uuid4())
        ham_id = str(uuid.uuid4())

        await qdrant_client.upsert(
            collection_name=TEST_COLLECTION,
            points=[
                qdrant_models.PointStruct(
                    id=spam_id,
                    vector=vector,
                    payload={
                        "mail_id": spam_id,
                        "account_id": account_id,
                        "is_spam": "true",
                    },
                ),
                qdrant_models.PointStruct(
                    id=ham_id,
                    vector=[v + 0.001 for v in vector],
                    payload={
                        "mail_id": ham_id,
                        "account_id": account_id,
                        "is_spam": "false",
                    },
                ),
            ],
        )

        # Search only for spam
        results = await qdrant_client.query_points(
            collection_name=TEST_COLLECTION,
            query=vector,
            query_filter=qdrant_models.Filter(
                must=[
                    qdrant_models.FieldCondition(
                        key="account_id",
                        match=qdrant_models.MatchValue(value=account_id),
                    ),
                    qdrant_models.FieldCondition(
                        key="is_spam",
                        match=qdrant_models.MatchValue(value="true"),
                    ),
                ]
            ),
            limit=10,
        )

        assert len(results.points) == 1
        assert str(results.points[0].id) == spam_id


class TestPayloadUpdate:
    """Test updating point payloads without re-embedding."""

    async def test_set_payload_updates_metadata(
        self, qdrant_client: AsyncQdrantClient, semantic_store: SemanticStore
    ) -> None:
        """Verify set_payload updates metadata in-place."""
        await semantic_store.ensure_collection()

        point_id = str(uuid.uuid4())
        vector = _random_vector()

        await qdrant_client.upsert(
            collection_name=TEST_COLLECTION,
            points=[
                qdrant_models.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "mail_id": point_id,
                        "account_id": "acc1",
                        "is_spam": "false",
                    },
                )
            ],
        )

        # Update is_spam tag
        await qdrant_client.set_payload(
            collection_name=TEST_COLLECTION,
            payload={"is_spam": "true"},
            points=[point_id],
        )

        # Verify update
        results = await qdrant_client.retrieve(
            collection_name=TEST_COLLECTION,
            ids=[point_id],
            with_payload=True,
        )
        assert results[0].payload is not None
        assert results[0].payload["is_spam"] == "true"
        # Original fields preserved
        assert results[0].payload["account_id"] == "acc1"
