"""
Job base class for background tasks.

Jobs snapshot settings at the start of each cycle (VEX H1).
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class JobStatus:
    """Status snapshot of a running job."""

    name: str
    account_id: uuid.UUID | None
    status: str
    cursor: dict[str, Any] | None = None
    last_run_at: datetime | None = None
    error_count: int = 0
    last_error: str | None = None


@dataclass
class JobConfig:
    """Configuration passed to a job on registration."""

    name: str
    account_id: uuid.UUID | None = None
    account_name: str | None = None
    enabled: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


class Job(ABC):
    """
    Abstract base for background jobs.

    Jobs read settings once per cycle (snapshot pattern) to avoid
    race conditions from mid-cycle settings changes.
    """

    def __init__(self, config: JobConfig) -> None:
        """
        Initialize the job.

        Args:
            config: Job configuration
        """
        self._config = config
        self._status = "idle"

    @property
    def name(self) -> str:
        """Job name identifier."""
        return self._config.name

    @property
    def account_id(self) -> uuid.UUID | None:
        """Account this job belongs to (None for global jobs)."""
        return self._config.account_id

    @property
    def enabled(self) -> bool:
        """Whether this job is enabled."""
        return self._config.enabled

    @abstractmethod
    async def start(self, cursor: dict[str, Any] | None = None) -> None:
        """
        Start the job, optionally resuming from a cursor.

        Args:
            cursor: Previous cursor state from DB (for resume)
        """

    @abstractmethod
    async def stop(self) -> dict[str, Any] | None:
        """
        Stop the job gracefully.

        Returns:
            Current cursor state to persist to DB
        """

    def status(self) -> str:
        """Get current job status: idle, running, error."""
        return self._status
