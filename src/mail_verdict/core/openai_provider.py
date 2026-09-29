"""
OpenAI client cache.

Mirrors anthropic_provider.py: one lazily-built AsyncOpenAI client, rebuilt
only when the caller hands in a different API key or base_url than the one
it was built with. base_url is what lets the same client class reach a
"custom" OpenAI-compatible server rather than api.openai.com -- None keeps
the SDK's own default.
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
_client_identity: tuple[str, str | None] | None = None


def get_openai_client(api_key: str | None, base_url: str | None = None) -> AsyncOpenAI | None:
    """
    Get a client for the given API key and base_url, rebuilding only if
    either changed.

    Args:
        api_key: The resolved API key, or None if none is configured
        base_url: A "custom" provider's compatible-server address, or None
            to use the SDK's own default (real OpenAI)

    Returns:
        A client, or None if api_key is falsy
    """
    global _client, _client_identity
    if not api_key:
        _client = None
        _client_identity = None
        return None
    identity = (api_key, base_url)
    if _client is None or identity != _client_identity:
        _client = AsyncOpenAI(
            api_key=api_key, base_url=base_url,
            timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0,
        )
        _client_identity = identity
    return _client


def reset_openai_provider() -> None:
    """Reset the cached client. Useful for testing and shutdown."""
    global _client, _client_identity
    _client = None
    _client_identity = None
