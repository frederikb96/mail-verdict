"""
JobManager: registry and lifecycle manager for background jobs.

Tracks all registered jobs, persists cursor state to DB, and
provides API-accessible status/control.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import JobState
from mail_verdict.jobs.base import Job, JobStatus

logger = logging.getLogger(__name__)


class JobManager:
    """
    Registry and lifecycle manager for background jobs.

    Responsibilities:
    - Register jobs by name
    - Start/stop all or individual jobs
    - Persist and restore cursor state via JobState DB model
    - Provide status snapshots for API
    """

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize the job manager.

        Args:
            db: Database connection for cursor persistence
        """
        self._db = db
        self._jobs: dict[str, Job] = {}

    def register(self, job: Job) -> None:
        """
        Register a job.

        Args:
            job: Job instance to register
        """
        key = self._job_key(job.name, job.account_id)
        self._jobs[key] = job
        logger.info(
            "Job registered",
            extra={"job": job.name, "account_id": str(job.account_id) if job.account_id else None},
        )

    async def start_all(self) -> None:
        """Start all registered and enabled jobs, restoring cursors from DB."""
        for key, job in self._jobs.items():
            if not job.enabled:
                logger.info("Job disabled, skipping", extra={"job": key})
                continue
            try:
                cursor = await self._load_cursor(job.name, job.account_id)
                await job.start(cursor=cursor)
                await self._update_status(job.name, job.account_id, "running")
                logger.info("Job started", extra={"job": key})
            except Exception as exc:
                logger.error("Failed to start job", extra={"job": key, "error": str(exc)})
                await self._record_error(job.name, job.account_id, str(exc))

    async def stop_all(self) -> None:
        """Gracefully stop all running jobs, persisting cursors."""
        for key, job in reversed(list(self._jobs.items())):
            try:
                cursor = await job.stop()
                await self._persist_cursor(job.name, job.account_id, cursor)
                await self._update_status(job.name, job.account_id, "idle")
                logger.info("Job stopped", extra={"job": key})
            except Exception as exc:
                logger.warning("Error stopping job", extra={"job": key, "error": str(exc)})

    async def start_job(self, name: str, account_id: uuid.UUID | None = None) -> bool:
        """
        Start a single job by name.

        Args:
            name: Job name
            account_id: Account scope (None for global)

        Returns:
            True if started successfully
        """
        key = self._job_key(name, account_id)
        job = self._jobs.get(key)
        if job is None:
            return False
        try:
            cursor = await self._load_cursor(name, account_id)
            await job.start(cursor=cursor)
            await self._update_status(name, account_id, "running")
            return True
        except Exception as exc:
            logger.error("Failed to start job", extra={"job": key, "error": str(exc)})
            await self._record_error(name, account_id, str(exc))
            return False

    async def stop_job(self, name: str, account_id: uuid.UUID | None = None) -> bool:
        """
        Stop a single job by name.

        Args:
            name: Job name
            account_id: Account scope (None for global)

        Returns:
            True if stopped successfully
        """
        key = self._job_key(name, account_id)
        job = self._jobs.get(key)
        if job is None:
            return False
        try:
            cursor = await job.stop()
            await self._persist_cursor(name, account_id, cursor)
            await self._update_status(name, account_id, "idle")
            return True
        except Exception as exc:
            logger.warning("Error stopping job", extra={"job": key, "error": str(exc)})
            return False

    def get_statuses(self) -> list[JobStatus]:
        """Get status snapshots for all registered jobs."""
        statuses: list[JobStatus] = []
        for job in self._jobs.values():
            statuses.append(
                JobStatus(
                    name=job.name,
                    account_id=job.account_id,
                    status=job.status(),
                )
            )
        return statuses

    async def get_statuses_from_db(self) -> list[JobStatus]:
        """Get job statuses from DB (includes persisted cursor/error info)."""
        async with self._db.session() as session:
            result = await session.execute(select(JobState).order_by(JobState.name))
            rows = list(result.scalars().all())
        return [
            JobStatus(
                name=row.name,
                account_id=row.account_id,
                status=row.status,
                cursor=row.cursor,
                last_run_at=row.last_run_at,
                error_count=row.error_count,
                last_error=row.last_error,
            )
            for row in rows
        ]

    async def _load_cursor(
        self, name: str, account_id: uuid.UUID | None,
    ) -> dict[str, Any] | None:
        """Load cursor from DB for a job."""
        async with self._db.session() as session:
            stmt = select(JobState).where(JobState.name == name)
            if account_id is not None:
                stmt = stmt.where(JobState.account_id == account_id)
            else:
                stmt = stmt.where(JobState.account_id.is_(None))
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
        return row.cursor if row else None

    async def _persist_cursor(
        self,
        name: str,
        account_id: uuid.UUID | None,
        cursor: dict[str, Any] | None,
    ) -> None:
        """Persist cursor to DB for a job."""
        async with self._db.session() as session:
            stmt = select(JobState).where(JobState.name == name)
            if account_id is not None:
                stmt = stmt.where(JobState.account_id == account_id)
            else:
                stmt = stmt.where(JobState.account_id.is_(None))
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row:
                row.cursor = cursor
                row.last_run_at = datetime.now(timezone.utc)
            else:
                session.add(JobState(
                    name=name,
                    account_id=account_id,
                    cursor=cursor,
                    status="idle",
                    last_run_at=datetime.now(timezone.utc),
                ))

    async def _update_status(
        self, name: str, account_id: uuid.UUID | None, status: str,
    ) -> None:
        """Update job status in DB."""
        async with self._db.session() as session:
            stmt = select(JobState).where(JobState.name == name)
            if account_id is not None:
                stmt = stmt.where(JobState.account_id == account_id)
            else:
                stmt = stmt.where(JobState.account_id.is_(None))
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row:
                row.status = status
                row.last_run_at = datetime.now(timezone.utc)
            else:
                session.add(JobState(
                    name=name,
                    account_id=account_id,
                    status=status,
                    last_run_at=datetime.now(timezone.utc),
                ))

    async def _record_error(
        self, name: str, account_id: uuid.UUID | None, error: str,
    ) -> None:
        """Record an error for a job in DB."""
        async with self._db.session() as session:
            stmt = select(JobState).where(JobState.name == name)
            if account_id is not None:
                stmt = stmt.where(JobState.account_id == account_id)
            else:
                stmt = stmt.where(JobState.account_id.is_(None))
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row:
                row.status = "error"
                row.error_count = row.error_count + 1
                row.last_error = error
                row.last_run_at = datetime.now(timezone.utc)
            else:
                session.add(JobState(
                    name=name,
                    account_id=account_id,
                    status="error",
                    error_count=1,
                    last_error=error,
                    last_run_at=datetime.now(timezone.utc),
                ))

    @staticmethod
    def _job_key(name: str, account_id: uuid.UUID | None) -> str:
        """Build a unique key for the job registry."""
        if account_id:
            return f"{name}:{account_id}"
        return name


_job_manager: JobManager | None = None


def get_job_manager() -> JobManager:
    """
    Get the global JobManager.

    Raises:
        RuntimeError: If not initialized
    """
    if _job_manager is None:
        raise RuntimeError("JobManager not initialized")
    return _job_manager


def init_job_manager(db: DatabaseConnection) -> JobManager:
    """
    Initialize the global JobManager.

    Args:
        db: Database connection

    Returns:
        Initialized JobManager
    """
    global _job_manager
    _job_manager = JobManager(db)
    return _job_manager


def reset_job_manager() -> None:
    """Reset the global JobManager. Useful for testing."""
    global _job_manager
    _job_manager = None
