"""
Embedding providers.

Mirrors spam/analyst.py's SpamAnalyst / LiveSpamAnalyst / FakeSpamAnalyst
shape: an abstract provider, a live implementation that resolves the
provider's API key fresh on every call rather than capturing it at
construction, and a deterministic fake for tests and API-key-free local
development.

Anthropic has no embedding model of its own and is not going to grow one --
its own documentation points at a third-party partner for this -- so it is
never one of the names `semantic.provider` accepts. "custom" is any
OpenAI-compatible embeddings endpoint (settings.semantic.base_url), the
same request shape as "openai" -- both are OpenAIEmbeddingProvider,
differing only in which client the resolved base_url points at.
"""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from mail_verdict.core.errors import ProviderUnavailableError
from mail_verdict.core.structured_llm import resolve_client
from mail_verdict.database.models import EMBEDDING_DIMENSIONS

if TYPE_CHECKING:
    from mail_verdict.settings.credentials import ProviderCredentialRepository

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


def resolve_active_embedding_model(settings: Mapping[str, Any]) -> str:
    """
    The embedding model actually serving search and neighbour hints right
    now, as opposed to `settings.semantic.model` -- the migration target a
    re-embed fills toward, which may still have incomplete coverage.

    `active_model` starts unset (None), meaning "whatever `model` currently
    is" -- the steady state where no migration is in flight. It is frozen
    to the previously-active model the moment `model` changes
    (api/settings_api.py's update_settings) and advanced to match `model`
    only once the reconciler observes full coverage under it
    (embeddings/worker.py's `_maybe_cutover`), which is what keeps search
    answering from a complete vector space throughout a migration.

    Args:
        settings: The "semantic" settings category

    Returns:
        The model name to embed a search query or neighbour lookup with
    """
    return str(settings.get("active_model") or settings.get("model") or DEFAULT_EMBEDDING_MODEL)


def resolve_active_embedding_provider(settings: Mapping[str, Any]) -> tuple[str, str | None]:
    """
    The provider and base_url a fresh search query must be embedded
    through, to land in the vector space `resolve_active_embedding_model`
    names -- which can differ from `settings.provider`/`base_url` mid
    migration if the switch changed provider as well as model (moving to
    a different compatible server, say). Frozen and advanced in the same
    write as `active_model` (see that function's docstring); falls back to
    the live provider/base_url exactly when `active_model` does, for the
    same reason.

    Args:
        settings: The "semantic" settings category

    Returns:
        (provider name, base_url or None)
    """
    active_provider = settings.get("active_provider")
    if active_provider:
        return str(active_provider), (settings.get("active_base_url") or None)
    return str(settings.get("provider") or "openai"), (settings.get("base_url") or None)


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
    Embeds via an OpenAI-compatible embeddings endpoint, truncated to
    EMBEDDING_DIMENSIONS via the API's own `dimensions` parameter.

    Serves both "openai" and "custom" (settings.semantic.provider): the
    request shape is identical, so only which client `resolve_client`
    hands back -- pointed at api.openai.com or at base_url -- differs.

    Every `text-embedding-3-*` model, and Infomaniak's own
    `Qwen/Qwen3-Embedding-8B`, supports Matryoshka truncation this way,
    which is what lets the vector column stay a single fixed width
    regardless of which model produced a given row -- the model itself is
    recorded per row instead (message_embeddings.model), so a model change
    is a visible coverage change rather than a validation the dimensions
    setting would otherwise need.
    """

    def __init__(
        self, cred_repo: ProviderCredentialRepository,
        *, provider: str = "openai", base_url: str | None = None,
    ) -> None:
        """
        Args:
            cred_repo: Provider API key repository, read fresh per call
            provider: "openai" or "custom" -- which credential to resolve
            base_url: Required when provider is "custom"; ignored otherwise
        """
        self._cred_repo = cred_repo
        self._provider = provider
        self._base_url = base_url

    async def embed_batch(self, texts: list[str], *, model: str) -> list[list[float]]:
        """
        Embed a batch, resolving the API key fresh.

        Raises whatever the client raises on failure -- rate limits,
        connection errors, and auth rejections are all `openai` exception
        types the worker's caller inspects directly, matching how
        core/structured_llm.py leaves provider exceptions to its callers
        rather than wrapping them here.
        """
        client = await resolve_client(self._provider, self._cred_repo, base_url=self._base_url)
        response = await client.embeddings.create(
            model=model, input=texts, dimensions=EMBEDDING_DIMENSIONS,
        )
        logger.debug(
            "Embedded batch",
            extra={"provider": self._provider, "model": model, "count": len(texts)},
        )
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
    provider_name: str, cred_repo: ProviderCredentialRepository, *, base_url: str | None = None,
) -> EmbeddingProvider:
    """
    Resolve a provider instance by name.

    Args:
        provider_name: "openai", "custom" or "fake"
        cred_repo: Provider API key repository
        base_url: Required when provider_name is "custom"; ignored
            otherwise

    Returns:
        A provider instance

    Raises:
        ValueError: provider_name is not recognized
    """
    if provider_name in ("openai", "custom"):
        return OpenAIEmbeddingProvider(cred_repo, provider=provider_name, base_url=base_url)
    if provider_name == "fake":
        return FakeEmbeddingProvider()
    raise ValueError(f"Unknown embedding provider {provider_name!r}")


__all__ = [
    "DEFAULT_EMBEDDING_MODEL",
    "EmbeddingProvider",
    "FakeEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "ProviderUnavailableError",
    "resolve_active_embedding_model",
    "resolve_active_embedding_provider",
    "resolve_embedding_provider",
]
