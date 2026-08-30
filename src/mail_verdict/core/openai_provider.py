"""
OpenAI client cache.

Mirrors anthropic_provider.py: one lazily-built AsyncOpenAI client, rebuilt
only when the caller hands in a different API key than the one it was
built with.
"""

from __future__ import annotations

from openai import AsyncOpenAI

# This client is shared by two callers with different leases: the
# classify stage (pipeline_runs, 120s by default) and the embedding
# worker (message_embeddings, 30s -- see embeddings/worker.py). A request
# that outlives its caller's lease is what lets a reclaim re-run it while
# the first call is still in flight, so the bound here is set against the
# tighter of the two rather than either alone. The SDK's own retries are
# turned off in favour of the app's single, already-jittered retry layer
# (core/structured_llm.py, core/retry.py) -- two independent retry loops
# would only stack latency on top of each other without adding safety.
REQUEST_TIMEOUT_SECONDS = 20.0

_client: AsyncOpenAI | None = None
_client_key: str | None = None


def get_openai_client(api_key: str | None) -> AsyncOpenAI | None:
    """
    Get a client for the given API key, rebuilding only if the key changed.

    Args:
        api_key: The resolved OpenAI API key, or None if none is configured

    Returns:
        A client, or None if api_key is falsy
    """
    global _client, _client_key
    if not api_key:
        _client = None
        _client_key = None
        return None
    if _client is None or api_key != _client_key:
        _client = AsyncOpenAI(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0)
        _client_key = api_key
    return _client


def reset_openai_provider() -> None:
    """Reset the cached client. Useful for testing and shutdown."""
    global _client, _client_key
    _client = None
    _client_key = None
