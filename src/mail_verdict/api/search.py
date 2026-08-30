"""
Search API endpoint.

GET /api/search — full-text search (PostgreSQL tsvector).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Query
from sqlalchemy import select

from mail_verdict.api.deps import get_message_repo
from mail_verdict.api.schemas import SearchResponse, SearchResult
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Account, Message

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
    across every account and merges results by relevance rank.
    """
    results = await _fulltext_search(q, account_id, limit)

    return SearchResponse(
        results=results[:limit],
        total=len(results),
        mode="fulltext",
        query=q,
    )


async def _fulltext_search(
    query: str,
    account_id: uuid.UUID | None,
    limit: int,
) -> list[SearchResult]:
    """Run PostgreSQL full-text search, scoped to one or all accounts."""
    msg_repo = get_message_repo()

    if account_id is not None:
        messages = await msg_repo.search_fulltext(account_id, query, limit=limit)
        return [_to_result(msg, i, len(messages)) for i, msg in enumerate(messages)]

    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Account.id))
        account_ids = [row[0] for row in result.all()]

    all_results: list[SearchResult] = []
    for aid in account_ids:
        messages = await msg_repo.search_fulltext(aid, query, limit=limit)
        all_results.extend(_to_result(msg, i, len(messages)) for i, msg in enumerate(messages))
    all_results.sort(key=lambda r: r.score, reverse=True)
    return all_results[:limit]


def _to_result(msg: Message, rank_index: int, total: int) -> SearchResult:
    """Build a SearchResult from a ranked message row."""
    score = 1.0 - (rank_index / max(total, 1))
    return SearchResult(
        message_id=msg.id,
        subject=msg.subject,
        from_addr=msg.from_addr,
        received_at=msg.received_at,
        score=score,
        source="fulltext",
    )
