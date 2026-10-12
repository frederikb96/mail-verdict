"""
Semantic layer API.

GET  /api/embeddings/status    -- coverage for the currently configured model
POST /api/embeddings/backfill  -- enqueue every message missing a current
                                   embedding, right now, rather than waiting
                                   out the periodic reconciler
GET  /api/embeddings/search    -- semantic search: embeds the query text
                                   and returns the likely hits, then the
                                   less likely ones, each newest first

Start/stop and concurrency for the embedding queue itself are not
duplicated here -- it registers under the name "embeddings" with the
generic queue API (GET/PATCH /api/queues/embeddings), the same surface
every other named queue uses.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from mail_verdict.api.schemas import (
    EmbeddingStatusResponse,
    SemanticSearchHit,
    SemanticSearchResponse,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.repository import list_row_marks
from mail_verdict.embeddings.provider import (
    DEFAULT_EMBEDDING_MODEL,
    resolve_active_embedding_model,
)
from mail_verdict.embeddings.query import SemanticUnavailableError
from mail_verdict.embeddings.repository import EmbeddingRepository
from mail_verdict.embeddings.service import search_by_meaning
from mail_verdict.settings.service import get_settings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/embeddings", tags=["embeddings"])

# The detail of the 503 a search answers when the query could not be
# embedded in time (provider rate limit, error or timeout, or no provider
# configured); clients keep their text results and say semantic search is
# unavailable.
SEMANTIC_UNAVAILABLE = "semantic_unavailable"

# One enqueue call reconsiders this many candidates; the loop below keeps
# calling until a call returns fewer than this, which is what makes it
# terminate on an ordinary mailbox instead of needing a manual cap.
_BACKFILL_BATCH_SIZE = 500


def _current_model() -> str:
    """The embedding model currently configured, read fresh."""
    settings = get_settings_service().get("semantic")
    return str(settings.get("model", DEFAULT_EMBEDDING_MODEL))


async def _status_response(
    repo: EmbeddingRepository, *, model: str, account_id: uuid.UUID | None,
) -> EmbeddingStatusResponse:
    """
    Build the full response for one model's coverage, including whether
    it is the one actually serving search right now and -- only when
    reporting the true, unscoped deployment state a real cutover decision
    is made against -- why it hasn't cut over yet, if it hasn't.

    cutover_ready/cutover_blocked_reason are left unset for an
    account-scoped query: cutover itself is deployment-wide (there is no
    per-account settings mechanism here), so a per-account readiness
    figure would not correspond to what the real gate actually checks.
    """
    semantic_settings = get_settings_service().get("semantic")
    active_model = resolve_active_embedding_model(semantic_settings)
    status = await repo.status(model=model, account_id=account_id)
    cutover_ready: bool | None = None
    cutover_blocked_reason: str | None = None
    if account_id is None and model != active_model:
        readiness = await repo.cutover_readiness(target_model=model, active_model=active_model)
        cutover_ready = readiness.ready
        cutover_blocked_reason = readiness.blocked_reason
    return EmbeddingStatusResponse(
        model=status.model, in_scope=status.in_scope, encoded=status.encoded,
        pending=status.pending, failed=status.failed,
        reachable=status.reachable, unreachable=status.unreachable, shadowed=status.shadowed,
        coverage=status.coverage, active=model == active_model, outstanding=status.outstanding,
        cutover_ready=cutover_ready, cutover_blocked_reason=cutover_blocked_reason,
    )


@router.get("/status", response_model=EmbeddingStatusResponse)
async def get_status(
    account_id: uuid.UUID | None = Query(default=None),
    model: str | None = Query(default=None),
) -> EmbeddingStatusResponse:
    """
    Coverage for one embedding model, defaulting to the configured one.

    Coverage below 100% is often the honest, permanent answer -- a real
    mailbox always has a few messages that never embed successfully -- so
    it is not what says whether a migration is ready to cut over;
    cutover_ready and cutover_blocked_reason are.
    """
    repo = EmbeddingRepository(get_db_connection())
    return await _status_response(repo, model=model or _current_model(), account_id=account_id)


@router.post("/backfill", response_model=EmbeddingStatusResponse)
async def trigger_backfill(
    account_id: uuid.UUID | None = Query(default=None),
) -> EmbeddingStatusResponse:
    """
    Enqueue every in-scope message missing a current-model embedding.

    Only inserts pending rows -- the embedding calls themselves happen
    asynchronously through the registered "embeddings" queue, so this
    returns quickly even over a large mailbox. The periodic reconciler
    (embeddings/worker.py) does the same thing on its own interval; this
    exists for an operator or a test that does not want to wait for it.
    """
    repo = EmbeddingRepository(get_db_connection())
    model = _current_model()
    while True:
        candidates, _ = await repo.enqueue_missing_batch(
            model=model, batch_size=_BACKFILL_BATCH_SIZE, account_id=account_id,
        )
        if candidates < _BACKFILL_BATCH_SIZE:
            break

    return await _status_response(repo, model=model, account_id=account_id)


@router.get("/search", response_model=SemanticSearchResponse)
async def search(
    q: str = Query(min_length=1),
    account_id: uuid.UUID | None = Query(default=None),
    folder_ids: list[uuid.UUID] | None = Query(
        default=None, description="Restrict to these folders; omit for no restriction",
    ),
    # Annotated, not `= Query(default=...)`: see the identical comment in
    # api/search.py -- keeps a direct call's real default a plain Python
    # value rather than an unresolved FastAPI descriptor.
    received_after: Annotated[
        datetime | None, Query(description="Only messages received at or after this instant"),
    ] = None,
    received_before: Annotated[
        datetime | None, Query(description="Only messages received at or before this instant"),
    ] = None,
) -> SemanticSearchResponse:
    """
    Semantic search: the messages closest in meaning to the query text,
    in two tiers -- likely hits first, then less likely ones, each newest
    first (see embeddings/search.py for how the tiers are drawn). The
    received_after/received_before window narrows the candidates before
    tiering. Single-page: the tiering bounds the result set naturally.

    Complements full-text search (GET /api/search) rather than replacing
    it -- literal search wins for a known sender or an exact phrase,
    this wins for a half-remembered topic with no exact words in common.
    """
    try:
        model, hits = await search_by_meaning(
            get_db_connection(), q, account_id=account_id, folder_ids=folder_ids,
            received_after=received_after, received_before=received_before,
        )
    except SemanticUnavailableError:
        # The detail is the code clients match on; the cause is in the log.
        raise HTTPException(status_code=503, detail=SEMANTIC_UNAVAILABLE) from None

    async with get_db_connection().session() as session:
        marks = await list_row_marks(session, [hit.message.id for hit in hits])
    return SemanticSearchResponse(
        results=[
            SemanticSearchHit(
                has_attachments=marks[hit.message.id].has_attachments,
                verdict_is_spam=marks[hit.message.id].verdict_is_spam,
                id=hit.message.id, account_id=hit.message.account_id,
                folder_id=hit.message.folder_id, thread_id=hit.message.thread_id,
                subject=hit.message.subject, from_addr=hit.message.from_addr,
                to_addrs=hit.message.to_addrs, received_at=hit.message.received_at,
                is_seen=hit.message.is_seen, is_flagged=hit.message.is_flagged,
                is_answered=hit.message.is_answered, is_draft=hit.message.is_draft,
                pending_sync=False if hit.message.is_glacier else hit.message.imap_uid is None,
                is_truncated=hit.message.is_truncated, mirrored_at=hit.message.created_at,
                tier=hit.tier,
                is_glacier=hit.message.is_glacier,
            )
            for hit in hits
        ],
        query=q, model=model,
    )
