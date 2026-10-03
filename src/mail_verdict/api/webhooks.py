"""
Webhook delivery API: what a rule's webhook action has queued, retrying a
failed delivery, and the backfill over existing mail.

GET  /api/webhooks/deliveries               -- newest first; ?name= &status= &limit=
POST /api/webhooks/deliveries/{id}/retry    -- re-queue a failed delivery
POST /api/webhooks/{name}/retry-failed      -- re-queue every failed delivery of one webhook
POST /api/webhooks/{name}/backfill          -- queue the webhook for existing mail

Deliveries are made by the webhooks queue's worker (webhooks/worker.py);
nothing here makes a request to a webhook's URL. A delivery shows its
status and the last HTTP status or error class, never a header value or a
response body.

The URL a webhook posts to is whatever the rule says. The application has
no authentication of its own, so anyone who can reach it can create a rule
and make the server send a request -- including to addresses only the
server can reach. Keep it behind the authenticating proxy the README
describes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import WebhookDelivery
from mail_verdict.pipeline.revisions import PipelineRevisionRepository
from mail_verdict.webhooks import repository
from mail_verdict.webhooks.backfill import (
    BackfillCursor,
    WebhookNotConfiguredError,
    backfill_webhook,
    find_webhook,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_MAX_BACKFILL_MESSAGES = 5000


class WebhookDeliveryOut(BaseModel):
    """One queued or finished delivery."""

    id: uuid.UUID
    name: str
    account_id: uuid.UUID
    message_id: uuid.UUID | None
    origin: str
    status: str
    attempts: int
    http_status: int | None
    last_error: str | None
    next_attempt_at: datetime
    delivered_at: datetime | None
    created_at: datetime


class WebhookBackfillCursor(BaseModel):
    """Where a truncated backfill stopped."""

    received_at: datetime
    message_id: uuid.UUID


class WebhookBackfillRequest(BaseModel):
    """Which existing mail to queue the webhook for. `cursor` is the previous
    call's `next_cursor`; the same `since`, `until` and `limit` go with it."""

    since: datetime
    until: datetime | None = None
    limit: int = Field(default=1000, ge=1, le=_MAX_BACKFILL_MESSAGES)
    dry_run: bool = False
    cursor: WebhookBackfillCursor | None = None


class WebhookBackfillResponse(BaseModel):
    """What a backfill call found. When `truncated`, `next_cursor` goes into
    the next request to continue with the mail this call did not reach."""

    scanned: int
    matched: int
    queued: int
    already_queued: int
    truncated: bool
    next_cursor: WebhookBackfillCursor | None


async def _current_effect(name: str) -> Any:
    """The webhook action `name` as the live rules define it, or None when
    no enabled rule carries it (a retry then keeps the queued snapshot)."""
    definition = await PipelineRevisionRepository(get_db_connection()).current()
    if definition is None:
        return None
    try:
        return find_webhook(definition.stages, name)[1]
    except WebhookNotConfiguredError:
        return None


def _to_out(row: Any) -> WebhookDeliveryOut:
    return WebhookDeliveryOut.model_validate(row, from_attributes=True)


@router.get("/deliveries", response_model=list[WebhookDeliveryOut])
async def list_deliveries(
    name: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[WebhookDeliveryOut]:
    """List deliveries, newest first."""
    async with get_db_connection().session() as session:
        rows = await repository.list_deliveries(session, name=name, status=status, limit=limit)
    return [_to_out(r) for r in rows]


@router.post("/deliveries/{delivery_id}/retry", status_code=202)
async def retry_delivery(delivery_id: uuid.UUID) -> dict[str, str]:
    """Re-queue a failed delivery with a fresh attempt budget, aimed at the
    rule's current URL, method and headers. Only a failed delivery moves; a
    delivered one is never sent again."""
    async with get_db_connection().session() as session:
        row = await session.get(WebhookDelivery, delivery_id)
    if row is None:
        raise HTTPException(status_code=409, detail="no failed delivery with that id")
    effect = await _current_effect(row.name)
    async with get_db_connection().session() as session:
        if not await repository.requeue_failed(session, delivery_id, effect=effect):
            raise HTTPException(status_code=409, detail="no failed delivery with that id")
    return {"status": "queued"}


@router.post("/{name}/retry-failed", status_code=202)
async def retry_failed(name: str) -> dict[str, int]:
    """Re-queue every failed delivery of one webhook, as the single retry
    does for one. Returns how many moved."""
    effect = await _current_effect(name)
    async with get_db_connection().session() as session:
        return {"requeued": await repository.requeue_all_failed(session, name, effect=effect)}


@router.post("/{name}/backfill", response_model=WebhookBackfillResponse)
async def backfill(name: str, request: WebhookBackfillRequest) -> WebhookBackfillResponse:
    """Queue the named webhook for existing mail its rule matches, oldest
    first, skipping mail that already has a delivery."""
    db = get_db_connection()
    definition = await PipelineRevisionRepository(db).current()
    if definition is None or not definition.enabled:
        raise HTTPException(status_code=409, detail="the pipeline is not enabled")
    try:
        result = await backfill_webhook(
            db, definition.stages, name, since=request.since, until=request.until,
            limit=request.limit, dry_run=request.dry_run,
            after=(
                BackfillCursor(request.cursor.received_at, request.cursor.message_id)
                if request.cursor else None
            ),
        )
    except WebhookNotConfiguredError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return WebhookBackfillResponse(
        scanned=result.scanned, matched=result.matched, queued=result.queued,
        already_queued=result.already_queued, truncated=result.truncated,
        next_cursor=(
            WebhookBackfillCursor(
                received_at=result.next_cursor.received_at,
                message_id=result.next_cursor.message_id,
            ) if result.next_cursor else None
        ),
    )
