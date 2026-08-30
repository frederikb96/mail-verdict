"""
Unit tests for embeddings/provider.py's FakeEmbeddingProvider and provider
resolution -- the real OpenAIEmbeddingProvider is exercised in the `llm`
layer, which needs a real key.
"""

from __future__ import annotations

import pytest

from mail_verdict.database.models import EMBEDDING_DIMENSIONS
from mail_verdict.embeddings.provider import (
    FakeEmbeddingProvider,
    OpenAIEmbeddingProvider,
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
