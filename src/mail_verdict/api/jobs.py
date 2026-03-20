"""
Job API endpoints.

GET /api/jobs — list all job statuses
POST /api/jobs/{name}/start — start a job
POST /api/jobs/{name}/stop — stop a job
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from mail_verdict.jobs.manager import get_job_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobStatusResponse(BaseModel):
    """Job status for API response."""

    name: str
    account_id: uuid.UUID | None = None
    status: str
    cursor: dict[str, Any] | None = None
    last_run_at: str | None = None
    error_count: int = 0
    last_error: str | None = None


@router.get("", response_model=list[JobStatusResponse])
async def list_jobs() -> list[JobStatusResponse]:
    """List all job statuses from DB."""
    manager = get_job_manager()
    statuses = await manager.get_statuses_from_db()
    return [
        JobStatusResponse(
            name=s.name,
            account_id=s.account_id,
            status=s.status,
            cursor=s.cursor,
            last_run_at=s.last_run_at.isoformat() if s.last_run_at else None,
            error_count=s.error_count,
            last_error=s.last_error,
        )
        for s in statuses
    ]


@router.post("/{job_name}/start")
async def start_job(
    job_name: str,
    account_id: uuid.UUID | None = Query(default=None),
) -> dict[str, str]:
    """Start a registered job."""
    manager = get_job_manager()
    ok = await manager.start_job(job_name, account_id)
    if not ok:
        raise HTTPException(
            status_code=404, detail=f"Job '{job_name}' not found or failed to start",
        )
    return {"status": "started", "job": job_name}


@router.post("/{job_name}/stop")
async def stop_job(
    job_name: str,
    account_id: uuid.UUID | None = Query(default=None),
) -> dict[str, str]:
    """Stop a running job."""
    manager = get_job_manager()
    ok = await manager.stop_job(job_name, account_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Job '{job_name}' not found or failed to stop")
    return {"status": "stopped", "job": job_name}
