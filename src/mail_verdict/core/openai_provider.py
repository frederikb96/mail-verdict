"""
OpenAI client cache.

Mirrors anthropic_provider.py: one lazily-built AsyncOpenAI client, rebuilt
only when the caller hands in a different API key than the one it was
built with.
"""

from __future__ import annotations

from openai import AsyncOpenAI

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
        _client = AsyncOpenAI(api_key=api_key)
        _client_key = api_key
    return _client


def reset_openai_provider() -> None:
    """Reset the cached client. Useful for testing and shutdown."""
    global _client, _client_key
    _client = None
    _client_key = None
