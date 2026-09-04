"""
Semantic layer API.

GET  /api/embeddings/status    -- coverage for the currently configured model
POST /api/embeddings/backfill  -- enqueue every message missing a current
                                   embedding, right now, rather than waiting
                                   out the periodic reconciler
GET  /api/embeddings/search    -- semantic search: embeds the query text
                                   and returns the nearest messages

Start/stop and concurrency for the embedding queue itself are not
duplicated here -- it registers under the name "embeddings" with the
generic queue API (GET/PATCH /api/queues/embeddings), the same surface
every other named queue uses.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Query

from mail_verdict.api.schemas import (
    EmbeddingStatusResponse,
    SemanticSearchResponse,
    SemanticSearchResult,
)
from mail_verdict.core.errors import ProviderUnavailableError
from mail_verdict.database.connection import get_db_connection
from mail_verdict.embeddings.provider import DEFAULT_EMBEDDING_MODEL, resolve_embedding_provider
from mail_verdict.embeddings.repository import EmbeddingRepository
from mail_verdict.embeddings.search import semantic_search
from mail_verdict.settings.credentials import get_provider_credential_repo
from mail_verdict.settings.service import get_settings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/embeddings", tags=["embeddings"])

# One enqueue call reconsiders this many candidates; the loop below keeps
# calling until a call returns fewer than this, which is what makes it
# terminate on an ordinary mailbox instead of needing a manual cap.
_BACKFILL_BATCH_SIZE = 500


def _current_model() -> str:
    """The embedding model currently configured, read fresh."""
    settings = get_settings_service().get("semantic")
    return str(settings.get("model", DEFAULT_EMBEDDING_MODEL))


@router.get("/status", response_model=EmbeddingStatusResponse)
async def get_status(
    account_id: uuid.UUID | None = Query(default=None),
    model: str | None = Query(default=None),
) -> EmbeddingStatusResponse:
    """
    Coverage for one embedding model, defaulting to the configured one.

    Coverage below 100% is the honest answer, not something to infer from
    search quietly returning less than it should.
    """
    repo = EmbeddingRepository(get_db_connection())
    status = await repo.status(model=model or _current_model(), account_id=account_id)
    return EmbeddingStatusResponse(
        model=status.model, in_scope=status.in_scope, encoded=status.encoded,
        pending=status.pending, failed=status.failed, coverage=status.coverage,
    )


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

    status = await repo.status(model=model, account_id=account_id)
    return EmbeddingStatusResponse(
        model=status.model, in_scope=status.in_scope, encoded=status.encoded,
        pending=status.pending, failed=status.failed, coverage=status.coverage,
    )


@router.get("/search", response_model=SemanticSearchResponse)
async def search(
    q: str = Query(min_length=1),
    account_id: uuid.UUID | None = Query(default=None),
    folder_ids: list[uuid.UUID] | None = Query(
        default=None, description="Restrict to these folders; omit for no restriction",
    ),
    limit: int = Query(default=20, ge=1, le=200),
    min_similarity: float | None = Query(default=None, ge=0.0, le=1.0),
) -> SemanticSearchResponse:
    """
    Semantic search: nearest messages to the meaning of the query text.

    Complements full-text search (GET /api/search) rather than replacing
    it -- literal search wins for a known sender or an exact phrase,
    this wins for a half-remembered topic with no exact words in common.
    """
    model = _current_model()
    settings = get_settings_service().get("semantic")
    provider_name = str(settings.get("provider", "openai"))
    cred_repo = get_provider_credential_repo()

    try:
        provider = resolve_embedding_provider(provider_name, cred_repo)
        vectors = await provider.embed_batch([q], model=model)
    except ProviderUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except Exception as exc:
        logger.exception("Semantic search query embedding failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    hits = await semantic_search(
        get_db_connection(), query_vector=vectors[0], model=model,
        account_id=account_id, folder_ids=folder_ids, k=limit,
        min_similarity=min_similarity,
    )
    return SemanticSearchResponse(
        results=[
            SemanticSearchResult(
                message_id=hit.message.id, account_id=hit.message.account_id,
                folder_id=hit.message.folder_id,
                subject=hit.message.subject, from_addr=hit.message.from_addr,
                received_at=hit.message.received_at, similarity=hit.similarity,
                is_seen=hit.message.is_seen, is_flagged=hit.message.is_flagged,
            )
            for hit in hits
        ],
        query=q, model=model,
    )
