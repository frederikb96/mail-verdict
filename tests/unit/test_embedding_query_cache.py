"""embed_query_cached: a repeated search query does not call the provider again."""

from __future__ import annotations

import pytest

from mail_verdict.embeddings import query as query_module
from mail_verdict.embeddings.provider import EmbeddingProvider
from mail_verdict.embeddings.query import embed_query_cached, normalise_query


class CountingProvider(EmbeddingProvider):
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed_batch(self, texts: list[str], *, model: str) -> list[list[float]]:
        self.texts.extend(texts)
        return [[float(len(text)), 1.0] for text in texts]


@pytest.fixture(autouse=True)
def _empty_cache() -> None:
    query_module._cache.clear()


async def _embed(provider: CountingProvider, text: str, *, model: str = "m", size: int = 8):
    return await embed_query_cached(
        provider, provider_name="custom", base_url="http://x", model=model, text=text,
        max_entries=size,
    )


@pytest.mark.asyncio
async def test_the_second_identical_query_does_not_reach_the_provider() -> None:
    provider = CountingProvider()
    first = await _embed(provider, "invoice from the garage")
    second = await _embed(provider, "invoice from the garage")
    assert first == second
    assert provider.texts == ["invoice from the garage"]


@pytest.mark.asyncio
async def test_case_and_whitespace_variants_share_one_entry() -> None:
    provider = CountingProvider()
    await _embed(provider, "Invoice  Garage")
    await _embed(provider, "  invoice garage ")
    assert provider.texts == ["invoice garage"]
    assert normalise_query(" A\tb\n") == "a b"


@pytest.mark.asyncio
async def test_another_model_is_a_different_vector() -> None:
    provider = CountingProvider()
    await _embed(provider, "garage", model="a")
    await _embed(provider, "garage", model="b")
    assert provider.texts == ["garage", "garage"]


@pytest.mark.asyncio
async def test_the_least_recently_used_entry_is_evicted() -> None:
    provider = CountingProvider()
    await _embed(provider, "one", size=2)
    await _embed(provider, "two", size=2)
    await _embed(provider, "one", size=2)  # refreshes "one"
    await _embed(provider, "three", size=2)  # evicts "two"
    await _embed(provider, "one", size=2)
    assert provider.texts == ["one", "two", "three"]
    await _embed(provider, "two", size=2)
    assert provider.texts == ["one", "two", "three", "two"]


@pytest.mark.asyncio
async def test_a_failed_embedding_is_not_cached() -> None:
    class Failing(CountingProvider):
        async def embed_batch(self, texts: list[str], *, model: str) -> list[list[float]]:
            raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        await _embed(Failing(), "garage")
    provider = CountingProvider()
    await _embed(provider, "garage")
    assert provider.texts == ["garage"]
