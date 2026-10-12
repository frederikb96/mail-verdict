"""
Semantic search as one call: embed the query, find the pool, tier it.

The REST endpoint and the MCP tool both go through here, so they search the
same way under the same limits.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from mail_verdict.config import get_config
from mail_verdict.embeddings.query import embed_search_query
from mail_verdict.embeddings.search import SemanticSearchResult, semantic_search

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection


async def search_by_meaning(
    db: DatabaseConnection,
    text: str,
    *,
    account_id: uuid.UUID | None = None,
    folder_ids: list[uuid.UUID] | None = None,
    received_after: datetime | None = None,
    received_before: datetime | None = None,
) -> tuple[str, list[SemanticSearchResult]]:
    """
    Search the mailbox by the meaning of `text`.

    Returns:
        (the embedding model that served the search, tiered hits)

    Raises:
        SemanticUnavailableError: The query could not be embedded in time
    """
    model, query_vector = await embed_search_query(text)
    hits = await semantic_search(
        db, query_vector=query_vector, model=model,
        exact_scan_max_rows=get_config().search.exact_scan_max_rows,
        account_id=account_id, folder_ids=folder_ids,
        received_after=received_after, received_before=received_before,
    )
    return model, hits
