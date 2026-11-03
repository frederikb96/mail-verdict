"""
Token counts read off a provider's response, one shape for every provider.

`input` is every input token the request was billed for, cached ones
included; `cache_read` and `cache_write` are the cached subsets of it.
`output` is every output token, reasoning included where the provider
counts it there (OpenAI's and compatible servers' completion counts do).

Each provider names these differently:

- Anthropic Messages: `input_tokens` excludes the cache, so input is
  `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`.
- Chat Completions (OpenAI-compatible servers): `prompt_tokens` already
  includes `prompt_tokens_details.cached_tokens` and `.cache_write_tokens`.
- OpenAI Responses: `input_tokens` already includes
  `input_tokens_details.cached_tokens` and `.cache_write_tokens`.
- Embeddings: `prompt_tokens` only.

A response without a usage block counts as zero tokens: the call is still
recorded, so it shows up as a call even when its size is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class TokenUsage:
    """One response's tokens."""

    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0


def _count(obj: Any, *path: str) -> int:
    """The integer at `path` under `obj`, 0 when any step is missing or None."""
    for name in path:
        if obj is None:
            return 0
        obj = getattr(obj, name, None)
    return obj if isinstance(obj, int) else 0


def from_anthropic(response: Any) -> TokenUsage:
    """Tokens of an Anthropic Messages API response."""
    usage = getattr(response, "usage", None)
    cache_read = _count(usage, "cache_read_input_tokens")
    cache_write = _count(usage, "cache_creation_input_tokens")
    return TokenUsage(
        input=_count(usage, "input_tokens") + cache_read + cache_write,
        cache_read=cache_read,
        cache_write=cache_write,
        output=_count(usage, "output_tokens"),
    )


def from_chat_completions(response: Any) -> TokenUsage:
    """Tokens of a Chat Completions response (OpenAI or a compatible server)."""
    usage = getattr(response, "usage", None)
    return TokenUsage(
        input=_count(usage, "prompt_tokens"),
        cache_read=_count(usage, "prompt_tokens_details", "cached_tokens"),
        cache_write=_count(usage, "prompt_tokens_details", "cache_write_tokens"),
        output=_count(usage, "completion_tokens"),
    )


def from_responses(response: Any) -> TokenUsage:
    """Tokens of an OpenAI Responses API response."""
    usage = getattr(response, "usage", None)
    return TokenUsage(
        input=_count(usage, "input_tokens"),
        cache_read=_count(usage, "input_tokens_details", "cached_tokens"),
        cache_write=_count(usage, "input_tokens_details", "cache_write_tokens"),
        output=_count(usage, "output_tokens"),
    )


def from_embeddings(response: Any) -> TokenUsage:
    """Tokens of an embeddings response."""
    return TokenUsage(input=_count(getattr(response, "usage", None), "prompt_tokens"))


def provider_label(provider: str, base_url: str | None) -> str:
    """
    Who bills the call: the provider setting, with a "custom"
    OpenAI-compatible server named after its host where it is a known one.

    Args:
        provider: The task's provider setting ("anthropic", "openai", "custom")
        base_url: The compatible server's address, for "custom"

    Returns:
        "anthropic", "openai", "infomaniak", or "custom" for any other server
    """
    if provider != "custom":
        return provider
    host = urlsplit(base_url or "").hostname or ""
    if host == "infomaniak.com" or host.endswith(".infomaniak.com"):
        return "infomaniak"
    return "custom"
