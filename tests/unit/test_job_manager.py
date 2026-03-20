"""Tests for JobManager: registration, start/stop, status, cursor persistence."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.jobs.base import Job, JobConfig, JobStatus
from mail_verdict.jobs.manager import JobManager


class FakeJob(Job):
    """Test job implementation."""

    def __init__(self, config: JobConfig) -> None:
        """Initialize fake job."""
        super().__init__(config)
        self.started = False
        self.stopped = False
        self.start_cursor: dict[str, Any] | None = None

    async def start(self, cursor: dict[str, Any] | None = None) -> None:
        """Start the fake job."""
        self.started = True
        self.start_cursor = cursor
        self._status = "running"

    async def stop(self) -> dict[str, Any] | None:
        """Stop the fake job."""
        self.stopped = True
        self._status = "idle"
        return {"last_uid": 42}


def _make_db_mock() -> MagicMock:
    """Create a mock DatabaseConnection with session context manager."""
    db = MagicMock()
    session = AsyncMock()
    mock_result = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    session.execute = AsyncMock(return_value=mock_result)
    session.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    session.add = MagicMock()

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    db.session = MagicMock(return_value=cm)
    return db


class TestJobConfig:
    """Tests for JobConfig and JobStatus dataclasses."""

    def test_job_config_defaults(self) -> None:
        """JobConfig has sensible defaults."""
        config = JobConfig(name="test_job")
        assert config.name == "test_job"
        assert config.account_id is None
        assert config.enabled is True

    def test_job_config_with_account(self) -> None:
        """JobConfig can be scoped to an account."""
        acct_id = uuid.uuid4()
        config = JobConfig(name="imap_sync", account_id=acct_id, account_name="personal")
        assert config.account_id == acct_id

    def test_job_status_dataclass(self) -> None:
        """JobStatus holds expected fields."""
        status = JobStatus(name="test", account_id=None, status="running")
        assert status.name == "test"
        assert status.error_count == 0


class TestJobABC:
    """Tests for Job abstract base class."""

    def test_name_from_config(self) -> None:
        """Job name comes from config."""
        job = FakeJob(JobConfig(name="my_job"))
        assert job.name == "my_job"

    def test_initial_status_idle(self) -> None:
        """Job starts in idle status."""
        job = FakeJob(JobConfig(name="test"))
        assert job.status() == "idle"

    def test_enabled_from_config(self) -> None:
        """Job enabled state comes from config."""
        job_enabled = FakeJob(JobConfig(name="test", enabled=True))
        assert job_enabled.enabled is True
        job_disabled = FakeJob(JobConfig(name="test", enabled=False))
        assert job_disabled.enabled is False


class TestJobManager:
    """Tests for JobManager registration and lifecycle."""

    def test_register_job(self) -> None:
        """Jobs are registered and retrievable."""
        db = _make_db_mock()
        manager = JobManager(db)
        job = FakeJob(JobConfig(name="test_job"))
        manager.register(job)
        assert "test_job" in manager._jobs

    def test_register_per_account(self) -> None:
        """Per-account jobs use compound key."""
        db = _make_db_mock()
        manager = JobManager(db)
        acct_id = uuid.uuid4()
        job = FakeJob(JobConfig(name="imap_sync", account_id=acct_id))
        manager.register(job)
        assert f"imap_sync:{acct_id}" in manager._jobs

    def test_get_statuses(self) -> None:
        """get_statuses returns status for all registered jobs."""
        db = _make_db_mock()
        manager = JobManager(db)
        manager.register(FakeJob(JobConfig(name="job_a")))
        manager.register(FakeJob(JobConfig(name="job_b")))
        statuses = manager.get_statuses()
        assert len(statuses) == 2
        names = {s.name for s in statuses}
        assert names == {"job_a", "job_b"}

    @pytest.mark.asyncio
    async def test_start_all(self) -> None:
        """start_all starts all enabled jobs."""
        db = _make_db_mock()
        manager = JobManager(db)
        job1 = FakeJob(JobConfig(name="job1"))
        job2 = FakeJob(JobConfig(name="job2", enabled=False))
        manager.register(job1)
        manager.register(job2)
        await manager.start_all()
        assert job1.started is True
        assert job2.started is False

    @pytest.mark.asyncio
    async def test_stop_all(self) -> None:
        """stop_all stops all jobs and persists cursors."""
        db = _make_db_mock()
        manager = JobManager(db)
        job = FakeJob(JobConfig(name="test"))
        manager.register(job)
        await manager.start_all()
        await manager.stop_all()
        assert job.stopped is True

    @pytest.mark.asyncio
    async def test_start_job_single(self) -> None:
        """start_job starts a single job by name."""
        db = _make_db_mock()
        manager = JobManager(db)
        job = FakeJob(JobConfig(name="single"))
        manager.register(job)
        ok = await manager.start_job("single")
        assert ok is True
        assert job.started is True

    @pytest.mark.asyncio
    async def test_start_job_not_found(self) -> None:
        """start_job returns False for unknown job."""
        db = _make_db_mock()
        manager = JobManager(db)
        ok = await manager.start_job("nonexistent")
        assert ok is False

    @pytest.mark.asyncio
    async def test_stop_job_single(self) -> None:
        """stop_job stops a single job."""
        db = _make_db_mock()
        manager = JobManager(db)
        job = FakeJob(JobConfig(name="stoppable"))
        manager.register(job)
        await manager.start_all()
        ok = await manager.stop_job("stoppable")
        assert ok is True
        assert job.stopped is True

    def test_job_key_global(self) -> None:
        """Global job key is just the name."""
        assert JobManager._job_key("test", None) == "test"

    def test_job_key_per_account(self) -> None:
        """Per-account job key includes account_id."""
        acct_id = uuid.uuid4()
        assert JobManager._job_key("test", acct_id) == f"test:{acct_id}"
