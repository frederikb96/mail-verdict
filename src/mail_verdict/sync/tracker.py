"""
Per-account sync progress tracker.

Holds mutable sync state in memory (ephemeral — not persisted to DB).
Every update() call pushes an SSE event into the account's EventRing.
The SSE endpoint reads the tracker for initial state snapshots on connect.
"""

from __future__ import annotations

import logging
import time
import uuid
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mail_verdict.api.event_ring import EventRing

logger = logging.getLogger(__name__)


class SyncPhase(str, Enum):
    """Sync lifecycle phases."""

    IDLE = "idle"
    PREFLIGHT = "preflight"
    SYNCING = "syncing"
    COMPLETE = "complete"
    ERROR = "error"
    CANCELLED = "cancelled"


class SyncTracker:
    """
    Per-account mutable sync state.

    Tracks current sync progress and exposes derived properties.
    On every update(), pushes an event into the linked EventRing.
    """

    def __init__(self, account_id: uuid.UUID, event_ring: EventRing | None = None) -> None:
        """
        Initialize tracker for an account.

        Args:
            account_id: Account UUID
            event_ring: Optional EventRing for SSE event emission
        """
        self._account_id = account_id
        self._event_ring = event_ring

        # Mutable state
        self.phase: SyncPhase = SyncPhase.IDLE
        self.folder_name: str | None = None
        self.folder_index: int = 0
        self.folder_total: int = 0
        self.synced: int = 0
        self.total_messages: int = 0
        self.folder_synced: int = 0
        self.folder_messages: int = 0
        self.new_mails: int = 0
        self.errors: int = 0
        self.last_error: str | None = None
        self.started_at: float | None = None

    @property
    def account_id(self) -> uuid.UUID:
        """Account UUID this tracker belongs to."""
        return self._account_id

    @property
    def can_sync(self) -> bool:
        """Whether a new sync can be started."""
        return self.phase in (
            SyncPhase.IDLE, SyncPhase.COMPLETE, SyncPhase.ERROR, SyncPhase.CANCELLED,
        )

    @property
    def can_cancel(self) -> bool:
        """Whether the current sync can be cancelled."""
        return self.phase in (SyncPhase.PREFLIGHT, SyncPhase.SYNCING)

    @property
    def elapsed_s(self) -> float:
        """Seconds elapsed since sync started, or 0 if not running."""
        if self.started_at is None:
            return 0.0
        return round(time.monotonic() - self.started_at, 1)

    @property
    def progress_pct(self) -> float:
        """Overall sync progress percentage (0-100)."""
        if self.total_messages <= 0:
            return 0.0
        return round(min(self.synced / self.total_messages * 100, 100.0), 1)

    def update(self, **kwargs: Any) -> None:
        """
        Update tracker fields and emit an SSE event.

        Accepts any combination of state fields as keyword arguments.
        Automatically determines the SSE event type from the update context.

        Args:
            **kwargs: Field names and values to update
        """
        # Track phase change for event type determination
        old_phase = self.phase

        for key, value in kwargs.items():
            if hasattr(self, key) and not key.startswith("_"):
                setattr(self, key, value)
            else:
                logger.warning("Unknown tracker field: %s", key)

        # Auto-set started_at on preflight/syncing transition
        if "phase" in kwargs and kwargs["phase"] in (SyncPhase.PREFLIGHT, SyncPhase.SYNCING):
            if old_phase in (
                SyncPhase.IDLE, SyncPhase.COMPLETE, SyncPhase.ERROR, SyncPhase.CANCELLED,
            ):
                self.started_at = time.monotonic()

        # Determine event type and push to ring
        if self._event_ring is not None:
            event_type = self._determine_event_type(old_phase, kwargs)
            self._event_ring.add(
                account_id=self._account_id,
                event_type=event_type,
                data=self.to_dict(),
            )

    def reset(self) -> None:
        """Reset tracker to idle state (for next sync cycle)."""
        self.phase = SyncPhase.IDLE
        self.folder_name = None
        self.folder_index = 0
        self.folder_total = 0
        self.synced = 0
        self.total_messages = 0
        self.folder_synced = 0
        self.folder_messages = 0
        self.new_mails = 0
        self.errors = 0
        self.last_error = None
        self.started_at = None

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize tracker state to a dict for SSE transmission.

        Returns:
            Full snapshot including derived fields
        """
        return {
            "account_id": str(self._account_id),
            "phase": self.phase.value,
            "folder_name": self.folder_name,
            "folder_index": self.folder_index,
            "folder_total": self.folder_total,
            "synced": self.synced,
            "total_messages": self.total_messages,
            "folder_synced": self.folder_synced,
            "folder_messages": self.folder_messages,
            "new_mails": self.new_mails,
            "errors": self.errors,
            "last_error": self.last_error,
            "can_sync": self.can_sync,
            "can_cancel": self.can_cancel,
            "elapsed_s": self.elapsed_s,
            "progress_pct": self.progress_pct,
        }

    def _determine_event_type(self, old_phase: SyncPhase, updates: dict[str, Any]) -> str:
        """
        Determine SSE event type from the update context.

        Args:
            old_phase: Phase before this update
            updates: Fields being updated

        Returns:
            SSE event type string
        """
        new_phase = updates.get("phase")

        # Phase transition -> sync.state
        if new_phase is not None and new_phase != old_phase:
            if new_phase == SyncPhase.ERROR:
                return "sync.error"
            return "sync.state"

        # Folder change -> sync.folder
        if "folder_name" in updates and "folder_index" in updates:
            return "sync.folder"

        # Progress update -> sync.progress
        if "synced" in updates or "folder_synced" in updates:
            return "sync.progress"

        # Default to sync.state for other updates
        return "sync.state"
