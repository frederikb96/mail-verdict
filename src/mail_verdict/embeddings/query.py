"""
Embedding a search query, with a small in-process cache of the vectors.

The provider round trip is most of a fresh semantic search's latency, and
the same text comes back again and again: every change of the time window,
toggling semantic search off and on, and returning to the page with Back
all re-submit a query that was just embedded. Only queries go through here
-- stored message embeddings are always computed fresh from what is stored.

A vector is keyed by provider, endpoint, model and the whitespace-collapsed,
case-folded text: two spellings of one search are one search to a person,
while the same text under another model or endpoint is a different vector.
The normalised text is also what gets embedded, so a vector never depends on
which spelling arrived first.

A search never waits out a provider's backoff. A rate-limited call is retried
once after a short pause, and anything that still does not produce a vector
in time -- a second rate limit, a provider error, a timeout, no credential --
raises SemanticUnavailableError, which the endpoint answers with 503
semantic_unavailable.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING

import openai

from mail_verdict.config import get_config
from mail_verdict.core.errors import ProviderUnavailableError
from mail_verdict.embeddings.provider import (
    resolve_active_embedding_model,
    resolve_active_embedding_provider,
    resolve_embedding_provider,
)
from mail_verdict.settings.credentials import get_provider_credential_repo
from mail_verdict.settings.service import get_settings_service

if TYPE_CHECKING:
    from mail_verdict.embeddings.provider import EmbeddingProvider

logger = logging.getLogger(__name__)

_CacheKey = tuple[str, str, str, str]

_cache: OrderedDict[_CacheKey, list[float]] = OrderedDict()

# Models trained to embed a search query differently from the documents it
# searches for take a one-line task description in front of the query; the
# documents themselves are embedded bare.
QUERY_INSTRUCTIONS: dict[str, str] = {
    "Qwen/Qwen3-Embedding-8B": "Given a search query, retrieve the emails that are relevant to it",
}


class SemanticUnavailableError(Exception):
    """The query could not be embedded quickly enough to search by meaning."""


def normalise_query(text: str) -> str:
    """The text a query is cached and embedded under."""
    return " ".join(text.split()).casefold()


def format_query(model: str, normalised: str) -> str:
    """The text sent to the provider: the model's query instruction, if it
    has one, then the query."""
    instruction = QUERY_INSTRUCTIONS.get(model)
    return f"Instruct: {instruction}\nQuery: {normalised}" if instruction else normalised


async def _embed_once_retrying_rate_limit(
    provider: EmbeddingProvider, text: str, *, model: str, timeout: float, retry_delay: float,
) -> list[float]:
    for attempt in (1, 2):
        try:
            return (await provider.embed_batch([text], model=model, timeout=timeout))[0]
        except openai.RateLimitError:
            if attempt == 2:
                raise
            await asyncio.sleep(retry_delay)
    raise AssertionError("unreachable")  # pragma: no cover


async def embed_query_cached(
    provider: EmbeddingProvider,
    *,
    provider_name: str,
    base_url: str | None,
    model: str,
    text: str,
    max_entries: int,
    timeout: float,
    retry_delay: float,
) -> list[float]:
    """
    The query's vector, from the cache when the same search was embedded
    before, else from the provider (and then remembered, evicting the least
    recently used entry beyond max_entries). A failed embedding caches
    nothing.

    Raises:
        SemanticUnavailableError: The provider could not produce the vector
            within `timeout`, even after one retry of a rate limit
    """
    normalised = normalise_query(text)
    key = (provider_name, base_url or "", model, normalised)
    cached = _cache.get(key)
    if cached is not None:
        _cache.move_to_end(key)
        return cached
    try:
        vector = await _embed_once_retrying_rate_limit(
            provider, format_query(model, normalised), model=model,
            timeout=timeout, retry_delay=retry_delay,
        )
    except (openai.OpenAIError, ProviderUnavailableError) as exc:
        logger.warning(
            "Query embedding unavailable", extra={"model": model, "error": type(exc).__name__},
        )
        raise SemanticUnavailableError(str(exc)) from exc
    _cache[key] = vector
    _cache.move_to_end(key)
    while len(_cache) > max_entries:
        _cache.popitem(last=False)
    return vector


async def embed_search_query(text: str) -> tuple[str, list[float]]:
    """
    Embed a search query with the model and provider currently serving
    search, returning (model, vector).

    That is settings.semantic.model except mid-migration (see
    resolve_active_embedding_model): embedding the query with anything else
    would compare it against a vector space it was never placed in.

    Raises:
        SemanticUnavailableError: The query could not be embedded in time
    """
    settings = get_settings_service().get("semantic")
    model = resolve_active_embedding_model(settings)
    provider_name, base_url = resolve_active_embedding_provider(settings)
    search_config = get_config().search
    try:
        provider = resolve_embedding_provider(
            provider_name, get_provider_credential_repo(), base_url=base_url,
        )
    except ValueError as exc:
        raise SemanticUnavailableError(str(exc)) from exc
    vector = await embed_query_cached(
        provider, provider_name=provider_name, base_url=base_url, model=model, text=text,
        max_entries=search_config.query_cache_size,
        timeout=search_config.query_timeout_seconds,
        retry_delay=search_config.query_rate_limit_retry_seconds,
    )
    return model, vector
