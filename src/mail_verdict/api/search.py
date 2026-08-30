"""
Search API endpoint.

GET /api/search — full-text search (PostgreSQL tsvector), scoped to one
account or across all of them.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Query

from mail_verdict.api.deps import get_message_repo
from mail_verdict.api.schemas import SearchResponse, SearchResult

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=SearchResponse)
async def search_messages(
    q: str = Query(min_length=1),
    account_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> SearchResponse:
    """
    Search messages by full-text match against PostgreSQL's tsvector.

    Scopes to a single account when account_id is given; otherwise searches
    across every account.
    """
    msg_repo = get_message_repo()
    rows = await msg_repo.search_fulltext_with_snippet(account_id, q, limit=limit)

    results = [
        SearchResult(
            message_id=msg.id,
            subject=msg.subject,
            from_addr=msg.from_addr,
            received_at=msg.received_at,
            snippet=snippet,
        )
        for msg, snippet in rows
    ]

    return SearchResponse(results=results, total=len(results), query=q)
