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

# Only the classify stage uses this client, against pipeline_runs' lease
# (120s by default -- see pipeline/runner.py and settings/defaults.py's
# "pipeline" category). Half that lease leaves room for a heartbeat to
# still land and for the row to be written back before the lease would
# otherwise expire. The SDK's own retries are turned off in favour of the
# app's single, already-jittered retry layer (core/structured_llm.py,
# core/retry.py) -- two independent retry loops would only stack latency
# on top of each other without adding safety.
REQUEST_TIMEOUT_SECONDS = 60.0

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
        _client = AsyncAnthropic(
            api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0,
        )
        _client_key = api_key
    return _client


def reset_anthropic_provider() -> None:
    """Reset the cached client. Useful for testing and shutdown."""
    global _client, _client_key
    _client = None
    _client_key = None
