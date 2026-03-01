"""
Sync event types.

Simple typed dataclasses for sync-related events. These will be
consumed by the event bus (Block 7), but for now they're used
directly by the SyncManager.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class SyncEvent:
    """Base class for all sync events."""

    account_id: uuid.UUID
    folder_id: uuid.UUID
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class MailReceived(SyncEvent):
    """New mail detected in a folder."""

    uid: int = 0
    message_id: str | None = None


@dataclass
class MailDeleted(SyncEvent):
    """Mail UID no longer present in folder."""

    uid: int = 0


@dataclass
class MailMoved(SyncEvent):
    """Mail detected in a different folder (same message_id)."""

    uid: int = 0
    message_id: str | None = None
    from_folder_id: uuid.UUID | None = None


@dataclass
class MailTrashed(SyncEvent):
    """Mail moved to trash folder."""

    uid: int = 0
    message_id: str | None = None


@dataclass
class MailSpamDetected(SyncEvent):
    """Mail moved to spam/junk folder."""

    uid: int = 0
    message_id: str | None = None


@dataclass
class FlagsChanged(SyncEvent):
    """Mail flags changed (read/unread, flagged, etc.)."""

    uid: int = 0
    is_read: bool = False
    is_flagged: bool = False
