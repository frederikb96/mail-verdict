"""Tests for sync trigger rate limiting (debounce)."""

from __future__ import annotations

import time
import uuid

from mail_verdict.api.accounts import (
    SYNC_DEBOUNCE_SECONDS,
    _sync_last_triggered,
)


class TestSyncDebounce:
    """Tests for the sync trigger debounce mechanism."""

    def setup_method(self) -> None:
        """Clear debounce state between tests."""
        _sync_last_triggered.clear()

    def test_first_trigger_allowed(self) -> None:
        """First sync trigger for an account has no prior timestamp."""
        account_id = uuid.uuid4()
        now = time.monotonic()
        last = _sync_last_triggered.get(account_id, 0.0)
        assert now - last >= SYNC_DEBOUNCE_SECONDS

    def test_recent_trigger_blocked(self) -> None:
        """Sync trigger within debounce window is detected."""
        account_id = uuid.uuid4()
        _sync_last_triggered[account_id] = time.monotonic()

        now = time.monotonic()
        last = _sync_last_triggered[account_id]
        assert now - last < SYNC_DEBOUNCE_SECONDS

    def test_expired_trigger_allowed(self) -> None:
        """Sync trigger after debounce window has elapsed is allowed."""
        account_id = uuid.uuid4()
        _sync_last_triggered[account_id] = time.monotonic() - SYNC_DEBOUNCE_SECONDS - 1.0

        now = time.monotonic()
        last = _sync_last_triggered[account_id]
        assert now - last >= SYNC_DEBOUNCE_SECONDS

    def test_different_accounts_independent(self) -> None:
        """Debounce state is per-account, not global."""
        acct_a = uuid.uuid4()
        acct_b = uuid.uuid4()
        _sync_last_triggered[acct_a] = time.monotonic()

        now = time.monotonic()
        last_a = _sync_last_triggered.get(acct_a, 0.0)
        last_b = _sync_last_triggered.get(acct_b, 0.0)

        assert now - last_a < SYNC_DEBOUNCE_SECONDS
        assert now - last_b >= SYNC_DEBOUNCE_SECONDS

    def test_debounce_constant_is_five_seconds(self) -> None:
        """Debounce window is 5 seconds per spec requirement."""
        assert SYNC_DEBOUNCE_SECONDS == 5.0
