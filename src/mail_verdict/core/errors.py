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
