"""Tests for account state machine transitions."""

from __future__ import annotations

from mail_verdict.database.models import AccountState
from mail_verdict.jobs.state_machine import _VALID_TRANSITIONS


class TestValidTransitions:
    """Tests for state transition validation."""

    def test_created_to_syncing(self) -> None:
        """Created -> syncing is valid."""
        assert AccountState.SYNCING in _VALID_TRANSITIONS[AccountState.CREATED]

    def test_created_to_error(self) -> None:
        """Created -> error is valid."""
        assert AccountState.ERROR in _VALID_TRANSITIONS[AccountState.CREATED]

    def test_syncing_to_seeding(self) -> None:
        """Syncing -> seeding is valid."""
        assert AccountState.SEEDING in _VALID_TRANSITIONS[AccountState.SYNCING]

    def test_syncing_to_error(self) -> None:
        """Syncing -> error is valid."""
        assert AccountState.ERROR in _VALID_TRANSITIONS[AccountState.SYNCING]

    def test_seeding_to_active(self) -> None:
        """Seeding -> active is valid."""
        assert AccountState.ACTIVE in _VALID_TRANSITIONS[AccountState.SEEDING]

    def test_active_to_error(self) -> None:
        """Active -> error is valid."""
        assert AccountState.ERROR in _VALID_TRANSITIONS[AccountState.ACTIVE]

    def test_error_to_syncing(self) -> None:
        """Error -> syncing is valid (retry)."""
        assert AccountState.SYNCING in _VALID_TRANSITIONS[AccountState.ERROR]

    def test_invalid_created_to_active(self) -> None:
        """Created -> active is NOT valid (must go through syncing+seeding)."""
        assert AccountState.ACTIVE not in _VALID_TRANSITIONS[AccountState.CREATED]

    def test_invalid_syncing_to_active(self) -> None:
        """Syncing -> active is NOT valid (must go through seeding)."""
        assert AccountState.ACTIVE not in _VALID_TRANSITIONS[AccountState.SYNCING]

    def test_all_states_have_transitions(self) -> None:
        """Every AccountState has defined transitions."""
        for state in AccountState:
            assert state in _VALID_TRANSITIONS, f"Missing transitions for {state}"

    def test_all_states_can_reach_error(self) -> None:
        """Every non-error state can transition to error."""
        for state in AccountState:
            if state != AccountState.ERROR:
                assert AccountState.ERROR in _VALID_TRANSITIONS[state], (
                    f"{state} cannot reach ERROR"
                )


class TestAccountStateEnum:
    """Tests for AccountState enum values."""

    def test_expected_states(self) -> None:
        """All expected states exist."""
        values = {s.value for s in AccountState}
        assert values == {"created", "syncing", "seeding", "active", "error"}

    def test_string_values(self) -> None:
        """State values are lowercase strings."""
        for state in AccountState:
            assert state.value == state.value.lower()
