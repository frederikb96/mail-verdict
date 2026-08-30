"""
Anthropic client cache.

Holds one lazily-built AsyncAnthropic client, rebuilt only when the caller
hands in a different API key than the one it was built with. Callers
resolve the key fresh on every use (see settings/credentials.py) so a
rotated key takes effect on the next call rather than the next restart;
this module exists only to avoid rebuilding the underlying HTTP client on
every single message when the key hasn't changed.
"""

from __future__ import annotations

from anthropic import AsyncAnthropic

_client: AsyncAnthropic | None = None
_client_key: str | None = None


def get_anthropic_client(api_key: str | None) -> AsyncAnthropic | None:
    """
    Get a client for the given API key, rebuilding only if the key changed.

    Args:
        api_key: The resolved Anthropic API key, or None if none is configured

    Returns:
        A client, or None if api_key is falsy
    """
    global _client, _client_key
    if not api_key:
        _client = None
        _client_key = None
        return None
    if _client is None or api_key != _client_key:
        _client = AsyncAnthropic(api_key=api_key)
        _client_key = api_key
    return _client


def reset_anthropic_provider() -> None:
    """Reset the cached client. Useful for testing and shutdown."""
    global _client, _client_key
    _client = None
    _client_key = None
