"""
Unit tests for embeddings/provider.py's FakeEmbeddingProvider and provider
resolution -- the real OpenAIEmbeddingProvider is exercised in the `llm`
layer, which needs a real key.
"""

from __future__ import annotations

import pytest

from mail_verdict.database.models import EMBEDDING_DIMENSIONS
from mail_verdict.embeddings.provider import (
    DEFAULT_EMBEDDING_MODEL,
    FakeEmbeddingProvider,
    OpenAIEmbeddingProvider,
    resolve_active_embedding_model,
    resolve_active_embedding_provider,
    resolve_embedding_provider,
)


@pytest.mark.asyncio
async def test_fake_provider_produces_correct_dimensions() -> None:
    """Every vector must be exactly EMBEDDING_DIMENSIONS long -- pgvector's
    column type would reject anything else."""
    provider = FakeEmbeddingProvider()
    vectors = await provider.embed_batch(["hello world"], model="fake")
    assert len(vectors) == 1
    assert len(vectors[0]) == EMBEDDING_DIMENSIONS


@pytest.mark.asyncio
async def test_fake_provider_is_deterministic() -> None:
    """The same text must always produce the same vector, so tests
    asserting on distance/ordering are reproducible."""
    provider = FakeEmbeddingProvider()
    a = await provider.embed_batch(["same text"], model="fake")
    b = await provider.embed_batch(["same text"], model="fake")
    assert a[0] == b[0]


@pytest.mark.asyncio
async def test_fake_provider_differs_for_different_text() -> None:
    """Different inputs must not collide on the same vector."""
    provider = FakeEmbeddingProvider()
    a = await provider.embed_batch(["alpha"], model="fake")
    b = await provider.embed_batch(["beta"], model="fake")
    assert a[0] != b[0]


@pytest.mark.asyncio
async def test_fake_provider_embeds_a_whole_batch() -> None:
    """One call can embed several texts, each getting its own vector in order."""
    provider = FakeEmbeddingProvider()
    vectors = await provider.embed_batch(["one", "two", "three"], model="fake")
    assert len(vectors) == 3
    assert vectors[0] != vectors[1] != vectors[2]


def test_resolve_openai_provider() -> None:
    """The 'openai' name resolves to the real provider class."""
    provider = resolve_embedding_provider("openai", cred_repo=None)  # type: ignore[arg-type]
    assert isinstance(provider, OpenAIEmbeddingProvider)


def test_resolve_fake_provider() -> None:
    """The 'fake' name resolves to the deterministic test provider."""
    provider = resolve_embedding_provider("fake", cred_repo=None)  # type: ignore[arg-type]
    assert isinstance(provider, FakeEmbeddingProvider)


def test_resolve_unknown_provider_raises() -> None:
    """An unrecognized provider name is a configuration error, not a
    silent fallback to something else."""
    with pytest.raises(ValueError, match="Unknown embedding provider"):
        resolve_embedding_provider("anthropic", cred_repo=None)  # type: ignore[arg-type]


def test_resolve_custom_provider() -> None:
    """The 'custom' name resolves to the same class as 'openai', carrying
    its own base_url."""
    provider = resolve_embedding_provider(  # type: ignore[arg-type]
        "custom", cred_repo=None, base_url="https://example.test/v1",
    )
    assert isinstance(provider, OpenAIEmbeddingProvider)
    assert provider._provider == "custom"  # noqa: SLF001
    assert provider._base_url == "https://example.test/v1"  # noqa: SLF001


class TestResolveActiveEmbeddingModel:
    """resolve_active_embedding_model: which model actually serves search."""

    def test_falls_back_to_model_when_unset(self) -> None:
        assert resolve_active_embedding_model({"model": "text-embedding-3-small"}) == (
            "text-embedding-3-small"
        )

    def test_falls_back_to_default_when_nothing_set(self) -> None:
        assert resolve_active_embedding_model({}) == DEFAULT_EMBEDDING_MODEL

    def test_frozen_active_model_wins_over_the_migration_target(self) -> None:
        """Mid-migration, active_model differs from model -- and it is
        the one search must keep using."""
        settings = {"model": "new-model", "active_model": "old-model"}
        assert resolve_active_embedding_model(settings) == "old-model"


class TestResolveActiveEmbeddingProvider:
    def test_falls_back_to_current_provider_and_base_url(self) -> None:
        settings = {"provider": "custom", "base_url": "https://example.test/v1"}
        assert resolve_active_embedding_provider(settings) == (
            "custom", "https://example.test/v1",
        )

    def test_defaults_to_openai_with_no_provider_set(self) -> None:
        assert resolve_active_embedding_provider({}) == ("openai", None)

    def test_frozen_active_provider_wins_over_the_migration_target(self) -> None:
        """Mid-migration, the frozen provider/base_url -- not the new
        target -- is what a fresh search query must be embedded through,
        to land in the vector space active_model actually names."""
        settings = {
            "provider": "custom", "base_url": "https://new.example.test/v1",
            "active_provider": "openai", "active_base_url": None,
        }
        assert resolve_active_embedding_provider(settings) == ("openai", None)
