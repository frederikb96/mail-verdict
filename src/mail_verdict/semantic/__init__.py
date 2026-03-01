"""MailVerdict semantic search module (Qdrant + OpenAI embeddings)."""

from mail_verdict.semantic.indexer import run_initial_index
from mail_verdict.semantic.store import SearchResult, SemanticStore, get_semantic_store
from mail_verdict.semantic.worker import (
    EmbeddingWorker,
    EmbedItem,
    get_embedding_worker,
    init_embedding_worker,
    reset_embedding_worker,
)

__all__ = [
    "EmbedItem",
    "EmbeddingWorker",
    "SearchResult",
    "SemanticStore",
    "get_embedding_worker",
    "get_semantic_store",
    "init_embedding_worker",
    "reset_embedding_worker",
    "run_initial_index",
]
