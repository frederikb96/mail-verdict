"""
Anthropic client singleton.

Mirrors the shape of the settings-driven provider pattern this project
already uses: a lazily-created client, reset between requests/tests, backed
by ANTHROPIC_API_KEY from the environment (never stored in settings --
secrets don't live in Postgres).
"""

from __future__ import annotations

import os

from anthropic import AsyncAnthropic

_client: AsyncAnthropic | None = None
_client_initialized = False


def init_anthropic_provider() -> AsyncAnthropic | None:
    """
    Create the global Anthropic client if ANTHROPIC_API_KEY is set.

    Returns:
        The client, or None if no API key is configured
    """
    global _client, _client_initialized
    if os.environ.get("ANTHROPIC_API_KEY"):
        _client = AsyncAnthropic()
    else:
        _client = None
    _client_initialized = True
    return _client


def get_anthropic_client() -> AsyncAnthropic | None:
    """
    Get the global Anthropic client, initializing it on first call.

    Returns:
        The client, or None if no API key is configured
    """
    global _client_initialized
    if not _client_initialized:
        init_anthropic_provider()
    return _client


def reset_anthropic_provider() -> None:
    """Reset the cached client. Useful for testing."""
    global _client, _client_initialized
    _client = None
    _client_initialized = False
