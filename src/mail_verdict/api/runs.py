"""
Pipeline run observability -- the "is it moving" and "why did this
message get that treatment" surface.

GET  /api/runs                  -- list, filterable by status/account/mail
GET  /api/runs/{id}             -- one run's full trace
POST /api/runs/{id}/retry       -- re-queue a failed run
GET  /api/mails/{mail_id}/runs  -- every run for one message
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from mail_verdict.api.schemas import PipelineRunResponse
from mail_verdict.database.connection import get_db_connection

router = APIRouter(tags=["runs"])

_LIST_COLUMNS = (
    "id, account_id, msg_key, message_id, origin, apply, status, skip_reason, attempts, "
    "pipeline_rev, halted_at_stage, failed_stage, last_error, trace, model_calls, "
    "started_at, finished_at, created_at"
)


def _to_response(row: Any) -> PipelineRunResponse:
    return PipelineRunResponse(
        id=row.id, account_id=row.account_id, msg_key=row.msg_key, message_id=row.message_id,
        origin=row.origin, apply=row.apply, status=row.status, skip_reason=row.skip_reason,
        attempts=row.attempts, pipeline_rev=row.pipeline_rev, halted_at_stage=row.halted_at_stage,
        failed_stage=row.failed_stage, last_error=row.last_error, trace=row.trace or [],
        model_calls=row.model_calls, started_at=row.started_at, finished_at=row.finished_at,
        created_at=row.created_at,
    )


@router.get("/runs", response_model=list[PipelineRunResponse])
async def list_runs(
    status: str | None = Query(default=None),
    account_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[PipelineRunResponse]:
    """List pipeline runs, newest first, optionally filtered."""
    db = get_db_connection()
    conditions = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status is not None:
        conditions.append("status = :status")
        params["status"] = status
    if account_id is not None:
        conditions.append("account_id = :account_id")
        params["account_id"] = account_id
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    async with db.session() as session:
        result = await session.execute(
            text(
                f"SELECT {_LIST_COLUMNS} FROM pipeline_runs {where} "
                "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
            ),
            params,
        )
        return [_to_response(row) for row in result.all()]


@router.get("/runs/{run_id}", response_model=PipelineRunResponse)
async def get_run(run_id: uuid.UUID) -> PipelineRunResponse:
    """Get one run's full trace."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            text(f"SELECT {_LIST_COLUMNS} FROM pipeline_runs WHERE id = :id"), {"id": run_id},
        )
        row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _to_response(row)


@router.post("/runs/{run_id}/retry", response_model=PipelineRunResponse)
async def retry_run(run_id: uuid.UUID) -> PipelineRunResponse:
    """Re-queue a failed run -- clears the terminal status and error, and
    lets the ordinary claim/lease mechanics pick it up again."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            text(
                "UPDATE pipeline_runs SET status = 'pending', attempts = 0, last_error = NULL, "
                "failed_stage = NULL, next_attempt_at = now() "
                "WHERE id = :id AND status = 'failed' "
                f"RETURNING {_LIST_COLUMNS}"
            ),
            {"id": run_id},
        )
        row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No failed run with that id")
    return _to_response(row)


@router.get("/mails/{mail_id}/runs", response_model=list[PipelineRunResponse])
async def list_runs_for_mail(mail_id: uuid.UUID) -> list[PipelineRunResponse]:
    """Every pipeline run for one message's current row id -- "why did
    this message get that treatment"."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            text(
                f"SELECT {_LIST_COLUMNS} FROM pipeline_runs WHERE message_id = :mail_id "
                "ORDER BY created_at DESC"
            ),
            {"mail_id": mail_id},
        )
        return [_to_response(row) for row in result.all()]
