"""
Dynamic OpenAI client provider.

Reads api_key from DB settings on each access. Caches the client
and only recreates when the key changes. No app restart needed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai import AsyncOpenAI

    from mail_verdict.settings.service import SettingsService

logger = logging.getLogger(__name__)

_provider: OpenAIClientProvider | None = None


class OpenAIClientProvider:
    """Lazy OpenAI client that refreshes when the API key changes."""

    def __init__(self, settings_service: SettingsService) -> None:
        """Initialize with settings service reference."""
        self._settings = settings_service
        self._client: AsyncOpenAI | None = None
        self._current_key: str = ""

    def get_client(self) -> AsyncOpenAI | None:
        """Get the current OpenAI client, recreating if key changed."""
        ai = self._settings.get("ai")
        key = ai.get("api_key", "") if ai else ""
        if not key:
            return None
        if key != self._current_key:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=key)
            self._current_key = key
            logger.info("OpenAI client created/refreshed")
        return self._client


def init_openai_provider(settings_service: SettingsService) -> OpenAIClientProvider:
    """Initialize the global provider singleton."""
    global _provider
    _provider = OpenAIClientProvider(settings_service)
    return _provider


def get_openai_provider() -> OpenAIClientProvider | None:
    """Get the global provider."""
    return _provider


def get_openai_client() -> AsyncOpenAI | None:
    """Get the current OpenAI client from the global provider."""
    if _provider is None:
        return None
    return _provider.get_client()


def reset_openai_provider() -> None:
    """Reset the global provider (for testing/shutdown)."""
    global _provider
    _provider = None
