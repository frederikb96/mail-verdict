"""
Embedding a search query, with a small in-process cache of the vectors.

The provider round trip is nearly all of a semantic search's latency, and
the same text comes back again and again: every change of the time window,
toggling semantic search off and on, and returning to the page with Back
all re-submit a query that was just embedded. Only queries go through here
-- stored message embeddings are always computed fresh from what is stored.

A vector is keyed by provider, endpoint, model and the whitespace-collapsed,
case-folded text: two spellings of one search are one search to a person,
while the same text under another model or endpoint is a different vector.
The normalised text is also what gets embedded, so a vector never depends on
which spelling arrived first.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING

from mail_verdict.config import get_config
from mail_verdict.embeddings.provider import (
    resolve_active_embedding_model,
    resolve_active_embedding_provider,
    resolve_embedding_provider,
)
from mail_verdict.settings.credentials import get_provider_credential_repo
from mail_verdict.settings.service import get_settings_service

if TYPE_CHECKING:
    from mail_verdict.embeddings.provider import EmbeddingProvider

_CacheKey = tuple[str, str, str, str]

_cache: OrderedDict[_CacheKey, list[float]] = OrderedDict()


def normalise_query(text: str) -> str:
    """The text a query is cached and embedded under."""
    return " ".join(text.split()).casefold()


async def embed_query_cached(
    provider: EmbeddingProvider,
    *,
    provider_name: str,
    base_url: str | None,
    model: str,
    text: str,
    max_entries: int,
) -> list[float]:
    """
    The query's vector, from the cache when the same search was embedded
    before, else from the provider (and then remembered, evicting the least
    recently used entry beyond max_entries). A failed embedding caches
    nothing.
    """
    normalised = normalise_query(text)
    key = (provider_name, base_url or "", model, normalised)
    cached = _cache.get(key)
    if cached is not None:
        _cache.move_to_end(key)
        return cached
    vector = (await provider.embed_batch([normalised], model=model))[0]
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
        ProviderUnavailableError: The provider is not configured or reachable
    """
    settings = get_settings_service().get("semantic")
    model = resolve_active_embedding_model(settings)
    provider_name, base_url = resolve_active_embedding_provider(settings)
    provider = resolve_embedding_provider(
        provider_name, get_provider_credential_repo(), base_url=base_url,
    )
    vector = await embed_query_cached(
        provider, provider_name=provider_name, base_url=base_url, model=model, text=text,
        max_entries=get_config().search.query_cache_size,
    )
    return model, vector
