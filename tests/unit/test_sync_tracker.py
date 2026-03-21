"""Tests for SyncTracker: phase transitions, derived fields, event emission."""

from __future__ import annotations

import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from mail_verdict.sync.tracker import SyncPhase, SyncTracker


class TestSyncTrackerInit:
    """Tests for SyncTracker initialization."""

    def test_default_state(self) -> None:
        """Tracker starts in idle phase with zero counters."""
        account_id = uuid.uuid4()
        tracker = SyncTracker(account_id)

        assert tracker.phase == SyncPhase.IDLE
        assert tracker.folder_name is None
        assert tracker.folder_index == 0
        assert tracker.folder_total == 0
        assert tracker.synced == 0
        assert tracker.total_messages == 0
        assert tracker.folder_synced == 0
        assert tracker.folder_messages == 0
        assert tracker.new_mails == 0
        assert tracker.errors == 0
        assert tracker.last_error is None
        assert tracker.started_at is None

    def test_account_id_property(self) -> None:
        """Account ID is accessible via property."""
        account_id = uuid.uuid4()
        tracker = SyncTracker(account_id)
        assert tracker.account_id == account_id


class TestSyncTrackerDerivedFields:
    """Tests for computed properties."""

    def test_can_sync_idle(self) -> None:
        """Can sync when idle."""
        tracker = SyncTracker(uuid.uuid4())
        assert tracker.can_sync is True

    def test_can_sync_complete(self) -> None:
        """Can sync when complete."""
        tracker = SyncTracker(uuid.uuid4())
        tracker.phase = SyncPhase.COMPLETE
        assert tracker.can_sync is True

    def test_can_sync_error(self) -> None:
        """Can sync when in error state."""
        tracker = SyncTracker(uuid.uuid4())
        tracker.phase = SyncPhase.ERROR
        assert tracker.can_sync is True

    def test_can_sync_cancelled(self) -> None:
        """Can sync when cancelled."""
        tracker = SyncTracker(uuid.uuid4())
        tracker.phase = SyncPhase.CANCELLED
        assert tracker.can_sync is True

    def test_cannot_sync_syncing(self) -> None:
        """Cannot start sync while syncing."""
        tracker = SyncTracker(uuid.uuid4())
        tracker.phase = SyncPhase.SYNCING
        assert tracker.can_sync is False

    def test_cannot_sync_preflight(self) -> None:
        """Cannot start sync during preflight."""
        tracker = SyncTracker(uuid.uuid4())
        tracker.phase = SyncPhase.PREFLIGHT
        assert tracker.can_sync is False

    def test_can_cancel_syncing(self) -> None:
        """Can cancel during syncing."""
        tracker = SyncTracker(uuid.uuid4())
        tracker.phase = SyncPhase.SYNCING
        assert tracker.can_cancel is True

    def test_can_cancel_preflight(self) -> None:
        """Can cancel during preflight."""
        tracker = SyncTracker(uuid.uuid4())
        tracker.phase = SyncPhase.PREFLIGHT
        assert tracker.can_cancel is True

    def test_cannot_cancel_idle(self) -> None:
        """Cannot cancel when idle."""
        tracker = SyncTracker(uuid.uuid4())
        assert tracker.can_cancel is False

    def test_elapsed_s_no_start(self) -> None:
        """Elapsed is 0 when not started."""
        tracker = SyncTracker(uuid.uuid4())
        assert tracker.elapsed_s == 0.0

    def test_elapsed_s_with_start(self) -> None:
        """Elapsed increases after started_at is set."""
        tracker = SyncTracker(uuid.uuid4())
        tracker.started_at = time.monotonic() - 5.0
        assert tracker.elapsed_s >= 4.9

    def test_progress_pct_zero(self) -> None:
        """Progress is 0 when total_messages is 0."""
        tracker = SyncTracker(uuid.uuid4())
        assert tracker.progress_pct == 0.0

    def test_progress_pct_partial(self) -> None:
        """Progress reflects synced/total ratio."""
        tracker = SyncTracker(uuid.uuid4())
        tracker.synced = 50
        tracker.total_messages = 200
        assert tracker.progress_pct == 25.0

    def test_progress_pct_complete(self) -> None:
        """Progress caps at 100."""
        tracker = SyncTracker(uuid.uuid4())
        tracker.synced = 200
        tracker.total_messages = 200
        assert tracker.progress_pct == 100.0

    def test_progress_pct_over(self) -> None:
        """Progress caps at 100 even if synced > total."""
        tracker = SyncTracker(uuid.uuid4())
        tracker.synced = 250
        tracker.total_messages = 200
        assert tracker.progress_pct == 100.0


class TestSyncTrackerUpdate:
    """Tests for update() method."""

    def test_update_single_field(self) -> None:
        """Update a single field."""
        tracker = SyncTracker(uuid.uuid4())
        tracker.update(folder_name="INBOX")
        assert tracker.folder_name == "INBOX"

    def test_update_multiple_fields(self) -> None:
        """Update multiple fields at once."""
        tracker = SyncTracker(uuid.uuid4())
        tracker.update(
            phase=SyncPhase.SYNCING,
            folder_name="INBOX",
            folder_index=1,
            folder_total=5,
        )
        assert tracker.phase == SyncPhase.SYNCING
        assert tracker.folder_name == "INBOX"
        assert tracker.folder_index == 1
        assert tracker.folder_total == 5

    def test_update_ignores_unknown_fields(self) -> None:
        """Unknown field names are logged and ignored."""
        tracker = SyncTracker(uuid.uuid4())
        tracker.update(nonexistent_field="value")
        assert not hasattr(tracker, "nonexistent_field")

    def test_update_ignores_private_fields(self) -> None:
        """Private fields (starting with _) cannot be set via update()."""
        tracker = SyncTracker(uuid.uuid4())
        tracker.update(_event_ring="bad")
        # Should not change
        assert tracker._event_ring is None

    def test_auto_started_at_on_preflight(self) -> None:
        """started_at is auto-set on transition to preflight from idle."""
        tracker = SyncTracker(uuid.uuid4())
        assert tracker.started_at is None
        tracker.update(phase=SyncPhase.PREFLIGHT)
        assert tracker.started_at is not None

    def test_no_reset_started_at_on_syncing(self) -> None:
        """started_at is NOT reset when transitioning from preflight to syncing."""
        tracker = SyncTracker(uuid.uuid4())
        tracker.update(phase=SyncPhase.PREFLIGHT)
        first_started = tracker.started_at
        tracker.update(phase=SyncPhase.SYNCING)
        assert tracker.started_at == first_started

    def test_pushes_to_event_ring(self) -> None:
        """update() calls event_ring.add() when ring is set."""
        ring = MagicMock()
        ring.add.return_value = 1
        account_id = uuid.uuid4()
        tracker = SyncTracker(account_id, event_ring=ring)

        tracker.update(phase=SyncPhase.SYNCING)

        ring.add.assert_called_once()
        call_kwargs = ring.add.call_args[1]
        assert call_kwargs["account_id"] == account_id
        assert call_kwargs["event_type"] == "sync.state"


class TestSyncTrackerEventTypes:
    """Tests for event type determination."""

    def test_phase_change_emits_sync_state(self) -> None:
        """Phase change emits sync.state event."""
        ring = MagicMock()
        ring.add.return_value = 1
        tracker = SyncTracker(uuid.uuid4(), event_ring=ring)

        tracker.update(phase=SyncPhase.SYNCING)
        assert ring.add.call_args[1]["event_type"] == "sync.state"

    def test_error_phase_emits_sync_error(self) -> None:
        """Transition to error emits sync.error event."""
        ring = MagicMock()
        ring.add.return_value = 1
        tracker = SyncTracker(uuid.uuid4(), event_ring=ring)

        tracker.update(phase=SyncPhase.ERROR, last_error="Connection failed")
        assert ring.add.call_args[1]["event_type"] == "sync.error"

    def test_folder_change_emits_sync_folder(self) -> None:
        """Folder name+index update emits sync.folder event."""
        ring = MagicMock()
        ring.add.return_value = 1
        tracker = SyncTracker(uuid.uuid4(), event_ring=ring)
        tracker.phase = SyncPhase.SYNCING

        tracker.update(folder_name="INBOX", folder_index=1)
        assert ring.add.call_args[1]["event_type"] == "sync.folder"

    def test_progress_update_emits_sync_progress(self) -> None:
        """Synced count update emits sync.progress event."""
        ring = MagicMock()
        ring.add.return_value = 1
        tracker = SyncTracker(uuid.uuid4(), event_ring=ring)
        tracker.phase = SyncPhase.SYNCING

        tracker.update(synced=50)
        assert ring.add.call_args[1]["event_type"] == "sync.progress"


class TestSyncTrackerToDict:
    """Tests for serialization."""

    def test_to_dict_all_fields(self) -> None:
        """to_dict() includes all state and derived fields."""
        account_id = uuid.uuid4()
        tracker = SyncTracker(account_id)
        tracker.phase = SyncPhase.SYNCING
        tracker.folder_name = "INBOX"
        tracker.folder_index = 2
        tracker.folder_total = 5
        tracker.synced = 100
        tracker.total_messages = 500
        tracker.started_at = time.monotonic() - 10.0

        d = tracker.to_dict()

        assert d["account_id"] == str(account_id)
        assert d["phase"] == "syncing"
        assert d["folder_name"] == "INBOX"
        assert d["folder_index"] == 2
        assert d["folder_total"] == 5
        assert d["synced"] == 100
        assert d["total_messages"] == 500
        assert d["can_sync"] is False
        assert d["can_cancel"] is True
        assert d["progress_pct"] == 20.0
        assert d["elapsed_s"] >= 9.9

    def test_to_dict_idle(self) -> None:
        """to_dict() in idle state has correct derived values."""
        tracker = SyncTracker(uuid.uuid4())
        d = tracker.to_dict()

        assert d["phase"] == "idle"
        assert d["can_sync"] is True
        assert d["can_cancel"] is False
        assert d["progress_pct"] == 0.0
        assert d["elapsed_s"] == 0.0


class TestSyncTrackerReset:
    """Tests for reset()."""

    def test_reset_clears_all(self) -> None:
        """reset() returns tracker to idle with all counters zeroed."""
        tracker = SyncTracker(uuid.uuid4())
        tracker.phase = SyncPhase.SYNCING
        tracker.folder_name = "INBOX"
        tracker.synced = 100
        tracker.total_messages = 500
        tracker.started_at = time.monotonic()

        tracker.reset()

        assert tracker.phase == SyncPhase.IDLE
        assert tracker.folder_name is None
        assert tracker.synced == 0
        assert tracker.total_messages == 0
        assert tracker.started_at is None


class TestSyncPhaseEnum:
    """Tests for SyncPhase enum values."""

    def test_expected_phases(self) -> None:
        """All expected phases exist."""
        values = {p.value for p in SyncPhase}
        assert values == {"idle", "preflight", "syncing", "complete", "error", "cancelled"}

    def test_string_values(self) -> None:
        """Phase values are lowercase strings."""
        for phase in SyncPhase:
            assert phase.value == phase.value.lower()

    def test_str_enum(self) -> None:
        """SyncPhase is a str enum (can be compared to strings)."""
        assert SyncPhase.IDLE == "idle"
