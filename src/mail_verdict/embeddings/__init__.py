"""
Semantic layer: one vector per message, and search over it.

message_embeddings is the corpus and, via its status/attempts/lease
columns, its own work queue (queue/work_queue.py). embeddings/worker.py's
register_embeddings() is the seam a composed application lifespan wires
this package in through; nothing here reaches into the lifespan itself.
"""

from __future__ import annotations

from mail_verdict.embeddings.content import EmbeddingInput, build_embedding_input
from mail_verdict.embeddings.provider import (
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingProvider,
    FakeEmbeddingProvider,
    OpenAIEmbeddingProvider,
    resolve_embedding_provider,
)
from mail_verdict.embeddings.repository import EmbeddingRepository, EmbeddingStatus
from mail_verdict.embeddings.search import (
    SemanticSearchOutcome,
    SemanticSearchResult,
    semantic_search,
)
from mail_verdict.embeddings.worker import EmbeddingComponents, register_embeddings

__all__ = [
    "DEFAULT_EMBEDDING_MODEL",
    "EmbeddingComponents",
    "EmbeddingInput",
    "EmbeddingProvider",
    "EmbeddingRepository",
    "EmbeddingStatus",
    "FakeEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "SemanticSearchOutcome",
    "SemanticSearchResult",
    "build_embedding_input",
    "register_embeddings",
    "resolve_embedding_provider",
    "semantic_search",
]
