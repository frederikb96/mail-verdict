"""
Search API endpoint.

GET /api/search — fuzzy, folder- and field-scoped search, newest first,
keyset-paginated the same way GET /api/accounts/:id/messages is.

Complements the MCP search_mail tool (search_fulltext_with_snippet,
untouched here): that tool ranks by relevance over a stemmed tsquery,
this endpoint is the search page's own backend and needs a single,
predictable notion of "matches" once results are ordered by date rather
than relevance -- see search_messages's docstring.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from mail_verdict.api.deps import get_message_repo
from mail_verdict.api.schemas import SearchField, SearchResponse, SearchResult
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Message
from mail_verdict.database.repository import SEARCH_FIELDS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])


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
    Fuzzy search over the toggled fields, scoped to the given folders,
    newest first. See search_messages on MessageRepository for the
    matching rule itself; this endpoint only resolves the cursor and
    shapes the response, the same division mails.py's list endpoint uses.
    """
    msg_repo = get_message_repo()

    cursor_received_at, cursor_id = None, None
    if before is not None:
        # Same cursor-resolution shape as GET /accounts/{id}/messages: the
        # cursor is a message id, resolved to its (received_at, id) pair
        # once here rather than threading a raw id into the keyset compare.
        db = get_db_connection()
        async with db.session() as session:
            cursor_row = (
                await session.execute(
                    select(Message.received_at, Message.id).where(Message.id == before)
                )
            ).one_or_none()
        if cursor_row is None:
            raise HTTPException(
                status_code=400, detail=f"Invalid cursor: message {before} not found"
            )
        cursor_received_at, cursor_id = cursor_row

    field_set = frozenset(fields) if fields else SEARCH_FIELDS
    rows = await msg_repo.search_messages(
        account_id,
        q,
        folder_ids=folder_ids,
        fields=field_set,
        cursor_received_at=cursor_received_at,
        cursor_id=cursor_id,
        limit=limit + 1,
    )

    has_more = len(rows) > limit
    rows = rows[:limit]
    results = [
        SearchResult(
            message_id=msg.id,
            account_id=msg.account_id,
            folder_id=msg.folder_id,
            subject=msg.subject,
            from_addr=msg.from_addr,
            received_at=msg.received_at,
            snippet=snippet,
            is_seen=msg.is_seen,
            is_flagged=msg.is_flagged,
        )
        for msg, snippet in rows
    ]
    next_cursor = str(results[-1].message_id) if has_more and results else None

    return SearchResponse(results=results, has_more=has_more, next_cursor=next_cursor, query=q)
