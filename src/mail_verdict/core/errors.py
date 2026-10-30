"""
Shared exception types for the LLM provider layer.

A dedicated, narrow exception for "no provider client available" lets
callers degrade on exactly that condition (skip classification, log and
move on) without a bare `except Exception` that would also swallow a real
bug in the request itself.
"""

from __future__ import annotations


class ProviderUnavailableError(Exception):
    """Raised when no usable API key is configured for the selected provider."""


class ModelRefused(Exception):
    """The model declined to answer (Anthropic `stop_reason: "refusal"`, an
    OpenAI-compatible `message.refusal`). Retrying the same request asks the
    same question again, so it is never retried."""

    def __init__(self, category: str | None = None) -> None:
        super().__init__(f"refusal ({category})" if category else "refusal")
        self.category = category
