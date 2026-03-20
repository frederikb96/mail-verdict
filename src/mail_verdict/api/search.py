"""
Search API endpoint.

GET /api/search — full-text + semantic search with mode selection.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Query

from mail_verdict.api.deps import get_mail_repo
from mail_verdict.api.schemas import SearchResponse, SearchResult

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=SearchResponse)
async def search_mails(
    q: str = Query(min_length=1),
    mode: str = Query(default="fulltext", pattern="^(fulltext|semantic|combined)$"),
    account_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> SearchResponse:
    """
    Search mails by full-text, semantic similarity, or combined mode.

    - fulltext: PostgreSQL tsvector + pg_trgm search
    - semantic: Qdrant vector similarity search
    - combined: merge both result sets with score normalization
    """
    results: list[SearchResult] = []

    if mode in ("fulltext", "combined"):
        ft_results = await _fulltext_search(q, account_id, limit)
        results.extend(ft_results)

    if mode in ("semantic", "combined"):
        sem_results = await _semantic_search(q, account_id, limit)
        results.extend(sem_results)

    if mode == "combined":
        results = _merge_and_normalize(results, limit)
    else:
        results = results[:limit]

    return SearchResponse(
        results=results,
        total=len(results),
        mode=mode,
        query=q,
    )


async def _fulltext_search(
    query: str,
    account_id: uuid.UUID | None,
    limit: int,
) -> list[SearchResult]:
    """Run PostgreSQL full-text search."""
    if account_id is None:
        # Full-text search requires account scoping
        from sqlalchemy import select

        from mail_verdict.database.connection import get_db_connection
        from mail_verdict.database.models import Account

        db = get_db_connection()
        async with db.session() as session:
            result = await session.execute(select(Account.id))
            account_ids = [row[0] for row in result.all()]

        all_results: list[SearchResult] = []
        mail_repo = get_mail_repo()
        for aid in account_ids:
            mails = await mail_repo.search_fulltext(aid, query, limit=limit)
            for i, mail in enumerate(mails):
                score = 1.0 - (i / max(len(mails), 1))
                all_results.append(
                    SearchResult(
                        mail_id=mail.id,
                        subject=mail.subject,
                        from_addr=mail.from_addr,
                        received_at=mail.received_at,
                        score=score,
                        source="fulltext",
                    )
                )
        return all_results[:limit]

    mail_repo = get_mail_repo()
    mails = await mail_repo.search_fulltext(account_id, query, limit=limit)
    results: list[SearchResult] = []
    for i, mail in enumerate(mails):
        score = 1.0 - (i / max(len(mails), 1))
        results.append(
            SearchResult(
                mail_id=mail.id,
                subject=mail.subject,
                from_addr=mail.from_addr,
                received_at=mail.received_at,
                score=score,
                source="fulltext",
            )
        )
    return results


async def _semantic_search(
    query: str,
    account_id: uuid.UUID | None,
    limit: int,
) -> list[SearchResult]:
    """Run Qdrant semantic similarity search."""
    from mail_verdict.semantic.store import get_semantic_store

    try:
        store = get_semantic_store()
    except RuntimeError:
        logger.warning("SemanticStore not initialized, skipping semantic search")
        return []

    hits = await store.search(
        query,
        limit=limit,
        account_id=str(account_id) if account_id else None,
    )

    results: list[SearchResult] = []
    for hit in hits:
        try:
            mail_uuid = uuid.UUID(hit.mail_id)
        except ValueError:
            continue

        results.append(
            SearchResult(
                mail_id=mail_uuid,
                score=hit.score,
                source="semantic",
            )
        )

    # Enrich with mail metadata from DB
    if results:
        mail_repo = get_mail_repo()
        for r in results:
            if account_id:
                mail = await mail_repo.get_by_id(account_id, r.mail_id)
                if mail:
                    r.subject = mail.subject
                    r.from_addr = mail.from_addr
                    r.received_at = mail.received_at

    return results


def _merge_and_normalize(
    results: list[SearchResult],
    limit: int,
) -> list[SearchResult]:
    """
    Merge fulltext and semantic results, normalize scores to [0, 1].

    For mails appearing in both sets, scores are averaged.
    """
    by_mail: dict[uuid.UUID, SearchResult] = {}
    scores: dict[uuid.UUID, list[float]] = {}

    for r in results:
        if r.mail_id not in by_mail:
            by_mail[r.mail_id] = r
            scores[r.mail_id] = [r.score]
        else:
            scores[r.mail_id].append(r.score)
            by_mail[r.mail_id].source = "combined"

    for mail_id, score_list in scores.items():
        by_mail[mail_id].score = sum(score_list) / len(score_list)

    merged = sorted(by_mail.values(), key=lambda r: r.score, reverse=True)
    return merged[:limit]
