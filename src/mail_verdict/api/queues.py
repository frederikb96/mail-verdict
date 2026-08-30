"""
Queue lifecycle API.

GET   /api/queues            -- summary of every registered queue
GET   /api/queues/{name}     -- one queue's summary
PATCH /api/queues/{name}     -- change state/concurrency, or force its
                                 circuit breaker closed, live

A queue is registered by whichever module owns the table it claims from
(an embedding worker, a pipeline runner); a name nothing registered is a
404, not an empty stub, so a typo in a client reads as one.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from mail_verdict.api.schemas import (
    CircuitStatusResponse,
    QueueConcurrency,
    QueuePatchRequest,
    QueueResponse,
)
from mail_verdict.queue.manager import QueueSummary, get_queue_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/queues", tags=["queues"])


def _to_response(summary: QueueSummary) -> QueueResponse:
    """Map a QueueSummary onto its API response shape."""
    return QueueResponse(
        name=summary.name,
        state=summary.state,
        concurrency=QueueConcurrency(
            target=summary.concurrency_target,
            actual=summary.concurrency_actual,
            max_allowed=summary.max_allowed_concurrency,
        ),
        depth=summary.depth,
        circuit=CircuitStatusResponse(
            state=summary.circuit.state.value,
            reason=summary.circuit.reason,
            since=summary.circuit.since,
            retry_after=summary.circuit.retry_after,
        ),
    )


@router.get("", response_model=list[QueueResponse])
async def list_queues() -> list[QueueResponse]:
    """List every registered queue with its current state."""
    manager = get_queue_manager()
    return [_to_response(summary) for summary in await manager.list_summaries()]


@router.get("/{name}", response_model=QueueResponse)
async def get_queue(name: str) -> QueueResponse:
    """Get one queue's current state."""
    manager = get_queue_manager()
    try:
        summary = await manager.summary(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No queue named {name!r}") from None
    return _to_response(summary)


@router.patch("/{name}", response_model=QueueResponse)
async def patch_queue(name: str, request: QueuePatchRequest) -> QueueResponse:
    """
    Change a queue's state or concurrency, or force its circuit breaker
    closed.

    Concurrency is rejected with 400 if it -- combined with every other
    running queue's own -- would exceed what the database pool can
    actually support: a setting that cannot work should fail here, not
    silently degrade into starved HTTP handlers discovered much later.
    """
    manager = get_queue_manager()
    try:
        if request.reset_circuit:
            await manager.reset_circuit(name)
        summary = await manager.set_state(
            name, state=request.state, concurrency=request.concurrency,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No queue named {name!r}") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _to_response(summary)
