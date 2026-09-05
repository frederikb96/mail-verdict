"""
Search API endpoint.

GET /api/search — field- and folder-scoped search, ranked by field tier
then newest first, keyset-paginated the same way GET
/api/accounts/:id/messages is.

Complements the MCP search_mail tool (search_fulltext_with_snippet,
untouched here): that tool ranks by relevance over a stemmed tsquery,
this endpoint is the search page's own backend and needs a single,
predictable notion of "matches" once results are ranked by tier rather
than relevance -- see MessageRepository.search_messages's docstring.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Query

from mail_verdict.api.deps import get_message_repo
from mail_verdict.api.schemas import SearchField, SearchResponse, SearchResult
from mail_verdict.database.models import Message
from mail_verdict.database.repository import SEARCH_FIELDS, MessageRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])


def _to_search_result(msg: Message, snippet: str | None, tier: int) -> SearchResult:
    """A search hit is a MessageSummary plus how the query matched it --
    see SearchResult's docstring for why the two are one shape."""
    return SearchResult(
        id=msg.id,
        account_id=msg.account_id,
        folder_id=msg.folder_id,
        thread_id=msg.thread_id,
        subject=msg.subject,
        from_addr=msg.from_addr,
        to_addrs=msg.to_addrs,
        received_at=msg.received_at,
        is_seen=msg.is_seen,
        is_flagged=msg.is_flagged,
        is_answered=msg.is_answered,
        is_draft=msg.is_draft,
        snippet=snippet,
        pending_sync=msg.imap_uid is None,
        is_truncated=msg.is_truncated,
        mirrored_at=msg.created_at,
        match_tier=tier,
    )


@router.get("", response_model=SearchResponse)
async def search_messages(
    q: str = Query(min_length=1),
    account_id: uuid.UUID | None = Query(default=None),
    folder_ids: list[uuid.UUID] | None = Query(
        default=None, description="Restrict to these folders; omit for no restriction",
    ),
    fields: list[SearchField] | None = Query(
        default=None, description="Which parts to search; omit for all four",
    ),
    before: uuid.UUID | None = Query(
        default=None, description="Cursor: id of the last result in the previous page",
    ),
    limit: int = Query(default=50, ge=1, le=200),
) -> SearchResponse:
    """
    Search over the toggled fields, scoped to the given folders, ranked by
    field tier then newest first. See MessageRepository.search_messages
    for the matching and ranking rule itself; this endpoint resolves the
    cursor, falls back to the trigram tier when the primary stage's first
    page is empty, and shapes the response.
    """
    msg_repo: MessageRepository = get_message_repo()
    field_set = frozenset(fields) if fields else SEARCH_FIELDS

    tokens = await msg_repo.tokenize(q)
    if not tokens:
        # A query that is pure punctuation or otherwise tokenizes to
        # nothing has no lexemes to build a tsquery, a fallback trigram
        # comparison, or a total from -- this is the query's own "no
        # results" rather than a database round trip to discover it.
        return SearchResponse(results=[], has_more=False, next_cursor=None, query=q, total=0)

    cursor_received_at, cursor_id, cursor_tier = None, None, None
    if before is not None:
        cursor_row = await msg_repo.resolve_search_cursor(before, tokens)
        if cursor_row is None:
            raise HTTPException(
                status_code=400, detail=f"Invalid cursor: message {before} not found"
            )
        cursor_received_at, cursor_id, cursor_tier = cursor_row

    rows = await msg_repo.search_messages(
        account_id,
        tokens,
        folder_ids=folder_ids,
        fields=field_set,
        cursor_received_at=cursor_received_at,
        cursor_id=cursor_id,
        cursor_tier=cursor_tier,
        limit=limit + 1,
    )

    has_more = len(rows) > limit
    rows = rows[:limit]
    total = await msg_repo.count_search_candidates(
        account_id, tokens, folder_ids=folder_ids, fields=field_set,
    )

    if not rows and before is None:
        # The primary (tsquery prefix) stage found nothing on the very
        # first page -- try the trigram fallback tier (subject+from,
        # typo-tolerant). Never on a later page: a cursor here always
        # means the primary stage, whose page this is a continuation of.
        fallback_rows = await msg_repo.search_messages_fallback(
            account_id, tokens, folder_ids=folder_ids, limit=limit,
        )
        results = [_to_search_result(msg, snippet, 4) for msg, snippet in fallback_rows]
        # The fallback is a single, unpaginated page -- its own count is
        # exactly what's shown, not the (zero) primary total above.
        return SearchResponse(
            results=results, has_more=False, next_cursor=None, query=q, total=len(results),
        )

    results = [_to_search_result(msg, snippet, tier) for msg, snippet, tier in rows]
    next_cursor = str(results[-1].id) if has_more and results else None

    return SearchResponse(
        results=results, has_more=has_more, next_cursor=next_cursor, query=q, total=total,
    )
