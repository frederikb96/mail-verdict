"""embed_query_cached: instruction, cache, and a search that never waits out a rate limit."""

from __future__ import annotations

import httpx
import openai
import pytest

from mail_verdict.core.errors import ProviderUnavailableError
from mail_verdict.embeddings import query as query_module
from mail_verdict.embeddings.provider import EmbeddingProvider
from mail_verdict.embeddings.query import (
    QUERY_INSTRUCTIONS,
    SemanticUnavailableError,
    embed_query_cached,
    format_query,
    normalise_query,
)

QWEN = "Qwen/Qwen3-Embedding-8B"


def _rate_limit() -> openai.RateLimitError:
    request = httpx.Request("POST", "https://example.test/v1/embeddings")
    return openai.RateLimitError(
        "rate limited", response=httpx.Response(429, request=request), body=None,
    )


class ScriptedProvider(EmbeddingProvider):
    """Raises the scripted errors, one per call, then answers."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.texts: list[str] = []
        self.timeouts: list[float | None] = []

    async def embed_batch(
        self, texts: list[str], *, model: str, timeout: float | None = None,
    ) -> list[list[float]]:
        self.texts.extend(texts)
        self.timeouts.append(timeout)
        if self.errors:
            raise self.errors.pop(0)
        return [[float(len(text)), 1.0] for text in texts]


@pytest.fixture(autouse=True)
def _empty_cache() -> None:
    query_module._cache.clear()


async def _embed(
    provider: EmbeddingProvider, text: str, *, model: str = "m", size: int = 8,
) -> list[float]:
    return await embed_query_cached(
        provider, provider_name="custom", base_url="http://x", model=model, text=text,
        max_entries=size, timeout=10.0, retry_delay=0.0,
    )


@pytest.mark.asyncio
async def test_the_second_identical_query_does_not_reach_the_provider() -> None:
    provider = ScriptedProvider()
    first = await _embed(provider, "invoice from the garage")
    second = await _embed(provider, "invoice from the garage")
    assert first == second
    assert provider.texts == ["invoice from the garage"]


@pytest.mark.asyncio
async def test_case_and_whitespace_variants_share_one_entry() -> None:
    provider = ScriptedProvider()
    await _embed(provider, "Invoice  Garage")
    await _embed(provider, "  invoice garage ")
    assert provider.texts == ["invoice garage"]
    assert normalise_query(" A\tb\n") == "a b"


@pytest.mark.asyncio
async def test_another_model_is_a_different_vector() -> None:
    provider = ScriptedProvider()
    await _embed(provider, "garage", model="a")
    await _embed(provider, "garage", model="b")
    assert provider.texts == ["garage", "garage"]


@pytest.mark.asyncio
async def test_the_least_recently_used_entry_is_evicted() -> None:
    provider = ScriptedProvider()
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
    failing = ScriptedProvider(openai.APIConnectionError(request=httpx.Request("POST", "http://x")))
    with pytest.raises(SemanticUnavailableError):
        await _embed(failing, "garage")
    provider = ScriptedProvider()
    await _embed(provider, "garage")
    assert provider.texts == ["garage"]


@pytest.mark.asyncio
async def test_a_model_with_a_query_instruction_gets_it_before_the_query() -> None:
    provider = ScriptedProvider()
    await _embed(provider, "Invoice  Garage", model=QWEN)
    assert provider.texts == [f"Instruct: {QUERY_INSTRUCTIONS[QWEN]}\nQuery: invoice garage"]
    assert format_query("text-embedding-3-small", "invoice") == "invoice"


@pytest.mark.asyncio
async def test_the_provider_is_given_the_query_timeout() -> None:
    provider = ScriptedProvider()
    await _embed(provider, "garage")
    assert provider.timeouts == [10.0]


@pytest.mark.asyncio
async def test_a_rate_limit_is_retried_once() -> None:
    provider = ScriptedProvider(_rate_limit())
    vector = await _embed(provider, "garage")
    assert vector
    assert len(provider.texts) == 2


@pytest.mark.asyncio
async def test_a_rate_limit_twice_is_semantic_unavailable() -> None:
    provider = ScriptedProvider(_rate_limit(), _rate_limit())
    with pytest.raises(SemanticUnavailableError):
        await _embed(provider, "garage")
    assert len(provider.texts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        openai.APITimeoutError(request=httpx.Request("POST", "http://x")),
        ProviderUnavailableError("No custom API key configured"),
    ],
    ids=["timeout", "no-credential"],
)
async def test_anything_else_that_stops_the_embedding_is_semantic_unavailable(
    error: Exception,
) -> None:
    provider = ScriptedProvider(error)
    with pytest.raises(SemanticUnavailableError):
        await _embed(provider, "garage")
    assert len(provider.texts) == 1  # not retried: only a rate limit is
