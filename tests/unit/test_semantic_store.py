"""Tests for SemanticStore: embed, upsert, search, content-hash dedup."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.config.loader import AIConfig, QdrantConfig
from mail_verdict.semantic.store import SemanticStore


def _make_store(
    qdrant: MagicMock | None = None,
    openai: MagicMock | None = None,
) -> SemanticStore:
    """Create a SemanticStore with mock clients."""
    qdrant_config = QdrantConfig(host="localhost", port=6333, collection_name="test_collection")
    ai_config = AIConfig(
        provider="openai",
        model="gpt-4o-mini",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=1536,
    )
    q = qdrant or MagicMock()
    return SemanticStore(q, qdrant_config, ai_config, openai_client=openai)


class TestBuildEmbeddingText:
    """Tests for build_embedding_text static method."""

    def test_all_fields(self) -> None:
        """All fields are combined."""
        text = SemanticStore.build_embedding_text(
            from_addr="alice@example.com",
            subject="Hello",
            body_text="Body content here",
        )
        assert "From: alice@example.com" in text
        assert "Subject: Hello" in text
        assert "Body content here" in text

    def test_only_body(self) -> None:
        """Only body is included when other fields are None."""
        text = SemanticStore.build_embedding_text(None, None, "Just body")
        assert text == "Just body"

    def test_excerpt_length_truncates(self) -> None:
        """Body is truncated to excerpt_length."""
        long_body = "x" * 1000
        text = SemanticStore.build_embedding_text(None, None, long_body, excerpt_length=50)
        assert len(text) == 50

    def test_all_none(self) -> None:
        """All None fields returns empty string."""
        text = SemanticStore.build_embedding_text(None, None, None)
        assert text == ""


class TestContentHash:
    """Tests for _content_hash static method."""

    def test_deterministic(self) -> None:
        """Same input gives same hash."""
        h1 = SemanticStore._content_hash("hello")
        h2 = SemanticStore._content_hash("hello")
        assert h1 == h2

    def test_different_input(self) -> None:
        """Different input gives different hash."""
        h1 = SemanticStore._content_hash("hello")
        h2 = SemanticStore._content_hash("world")
        assert h1 != h2


class TestBuildPayload:
    """Tests for _build_payload static method."""

    def test_minimal_payload(self) -> None:
        """Builds payload with only required fields."""
        payload = SemanticStore._build_payload("mail-1", "acc-1", "hash-1")
        assert payload["mail_id"] == "mail-1"
        assert payload["account_id"] == "acc-1"
        assert payload["content_hash"] == "hash-1"
        assert "is_spam" not in payload
        assert "folder" not in payload

    def test_full_payload(self) -> None:
        """Builds payload with all optional fields."""
        dt = datetime(2024, 1, 15, tzinfo=timezone.utc)
        payload = SemanticStore._build_payload(
            "mail-1", "acc-1", "hash-1",
            is_spam=True,
            folder="INBOX",
            from_domain="example.com",
            received_at=dt,
        )
        assert payload["is_spam"] == "true"
        assert payload["folder"] == "INBOX"
        assert payload["from_domain"] == "example.com"
        assert "2024" in payload["received_at"]

    def test_is_spam_false(self) -> None:
        """is_spam=False becomes string 'false'."""
        payload = SemanticStore._build_payload("m", "a", "h", is_spam=False)
        assert payload["is_spam"] == "false"


class TestBuildSearchFilter:
    """Tests for _build_search_filter static method."""

    def test_no_filters(self) -> None:
        """No parameters returns None."""
        assert SemanticStore._build_search_filter() is None

    def test_account_filter(self) -> None:
        """account_id adds a must condition."""
        f = SemanticStore._build_search_filter(account_id="acc-1")
        assert f is not None
        assert f.must is not None
        assert len(f.must) == 1

    def test_exclude_ids(self) -> None:
        """exclude_ids adds a must_not condition."""
        f = SemanticStore._build_search_filter(exclude_ids=["id-1"])
        assert f is not None
        assert f.must_not is not None
        assert len(f.must_not) == 1


class TestEmbed:
    """Tests for embed method."""

    @pytest.mark.asyncio
    async def test_embed_calls_openai(self, mock_openai: MagicMock) -> None:
        """embed() calls OpenAI embeddings API."""
        store = _make_store(openai=mock_openai)
        result = await store.embed(["test text"])
        assert result is not None
        assert len(result) == 1
        assert len(result[0]) == 1536
        mock_openai.embeddings.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_embed_empty_list(self, mock_openai: MagicMock) -> None:
        """embed([]) returns empty list without API call."""
        store = _make_store(openai=mock_openai)
        result = await store.embed([])
        assert result == []
        mock_openai.embeddings.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_embed_failure_returns_none(self) -> None:
        """embed() returns None on API error."""
        mock_openai = MagicMock()
        mock_openai.embeddings = MagicMock()
        mock_openai.embeddings.create = AsyncMock(side_effect=RuntimeError("API down"))
        store = _make_store(openai=mock_openai)
        result = await store.embed(["test"])
        assert result is None


class TestSingleton:
    """Tests for singleton pattern."""

    def test_init_and_reset(self) -> None:
        """init_instance sets singleton, reset_instance clears it."""
        qdrant_config = QdrantConfig(host="localhost", port=6333, collection_name="test")
        ai_config = AIConfig(
            provider="openai", model="m", embedding_model="e", embedding_dimensions=1536
        )
        instance = SemanticStore.init_instance(MagicMock(), qdrant_config, ai_config)
        assert SemanticStore._instance is instance
        SemanticStore.reset_instance()
        assert SemanticStore._instance is None
