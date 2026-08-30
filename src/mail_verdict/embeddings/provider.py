"""
Embedding providers.

Mirrors spam/analyst.py's SpamAnalyst / LiveSpamAnalyst / FakeSpamAnalyst
shape: an abstract provider, a live implementation that resolves the
provider's API key fresh on every call rather than capturing it at
construction, and a deterministic fake for tests and API-key-free local
development.

There is deliberately only one live implementation. Anthropic has no
embedding model of its own and is not going to grow one -- its own
documentation points at a third-party partner for this -- so "provider" is
not a `semantic` settings knob the way it is for spam's `ai.provider`.
"""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from mail_verdict.core.errors import ProviderUnavailableError
from mail_verdict.core.structured_llm import resolve_client
from mail_verdict.database.models import EMBEDDING_DIMENSIONS

if TYPE_CHECKING:
    from mail_verdict.settings.credentials import ProviderCredentialRepository

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


class EmbeddingProvider(ABC):
    """Abstract base for turning text into vectors."""

    @abstractmethod
    async def embed_batch(self, texts: list[str], *, model: str) -> list[list[float]]:
        """
        Embed a batch of texts in one request.

        Args:
            texts: Texts to embed, in order
            model: Provider model name to embed with

        Returns:
            One vector per input text, same order, each of
            EMBEDDING_DIMENSIONS length

        Raises:
            ProviderUnavailableError: No API key configured
            RuntimeError: The request failed after retries
        """


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """
    Embeds via OpenAI's embeddings endpoint, truncated to
    EMBEDDING_DIMENSIONS via the API's own `dimensions` parameter.

    Every `text-embedding-3-*` model supports Matryoshka truncation this
    way, which is what lets the vector column stay a single fixed width
    regardless of which model produced a given row -- the model itself is
    recorded per row instead (message_embeddings.model), so a model change
    is a visible coverage change rather than a validation the dimensions
    setting would otherwise need.
    """

    def __init__(self, cred_repo: ProviderCredentialRepository) -> None:
        """
        Args:
            cred_repo: Provider API key repository, read fresh per call
        """
        self._cred_repo = cred_repo

    async def embed_batch(self, texts: list[str], *, model: str) -> list[list[float]]:
        """
        Embed a batch through OpenAI, resolving the API key fresh.

        Raises whatever the client raises on failure -- rate limits,
        connection errors, and auth rejections are all `openai` exception
        types the worker's caller inspects directly, matching how
        core/structured_llm.py leaves provider exceptions to its callers
        rather than wrapping them here.
        """
        client = await resolve_client("openai", self._cred_repo)
        response = await client.embeddings.create(
            model=model, input=texts, dimensions=EMBEDDING_DIMENSIONS,
        )
        logger.debug("Embedded batch", extra={"model": model, "count": len(texts)})
        return [item.embedding for item in response.data]


class FakeEmbeddingProvider(EmbeddingProvider):
    """
    Deterministic, hash-derived vectors -- the test workhorse.

    Never calls out to a real provider: each text's vector is derived from
    a SHA-256 hash of the text, so the same input always produces the same
    output and different inputs produce different (if meaningless) ones.
    Good enough to exercise storage, claim/complete, and cosine-distance
    ordering in tests without an API key.
    """

    async def embed_batch(self, texts: list[str], *, model: str) -> list[list[float]]:
        """Derive a deterministic vector per text from its hash."""
        return [_fake_vector(text) for text in texts]


def _fake_vector(text: str) -> list[float]:
    """A deterministic unit-ish vector derived from a text's hash, long
    enough to fill EMBEDDING_DIMENSIONS by repeating the digest bytes."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [
        (digest[i % len(digest)] / 255.0) * 2 - 1 for i in range(EMBEDDING_DIMENSIONS)
    ]


def resolve_embedding_provider(
    provider_name: str, cred_repo: ProviderCredentialRepository,
) -> EmbeddingProvider:
    """
    Resolve a provider instance by name.

    Args:
        provider_name: "openai" or "fake"
        cred_repo: Provider API key repository

    Returns:
        A provider instance

    Raises:
        ValueError: provider_name is not recognized
    """
    if provider_name == "openai":
        return OpenAIEmbeddingProvider(cred_repo)
    if provider_name == "fake":
        return FakeEmbeddingProvider()
    raise ValueError(f"Unknown embedding provider {provider_name!r}")


__all__ = [
    "DEFAULT_EMBEDDING_MODEL",
    "EmbeddingProvider",
    "FakeEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "ProviderUnavailableError",
    "resolve_embedding_provider",
]
