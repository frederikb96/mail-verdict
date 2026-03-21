"""Tests for SelectionManager: toggle, clear, count, properties, and global registry."""

from __future__ import annotations

import uuid

from mail_verdict.api.selection import (
    SelectionManager,
    _selection_managers,
    get_selection_manager,
)


class TestSelectionManagerToggle:
    """Tests for toggle() method."""

    def test_toggle_adds_mail(self) -> None:
        """Toggle on unselected mail adds it to selection."""
        manager = SelectionManager(uuid.uuid4())
        mail_id = uuid.uuid4()

        result = manager.toggle(mail_id)

        assert mail_id in result
        assert manager.count == 1

    def test_toggle_removes_selected_mail(self) -> None:
        """Toggle on already selected mail removes it."""
        manager = SelectionManager(uuid.uuid4())
        mail_id = uuid.uuid4()

        manager.toggle(mail_id)
        result = manager.toggle(mail_id)

        assert mail_id not in result
        assert manager.count == 0

    def test_toggle_tracks_last_toggled(self) -> None:
        """_last_toggled is updated on every toggle."""
        manager = SelectionManager(uuid.uuid4())
        mail_a = uuid.uuid4()
        mail_b = uuid.uuid4()

        manager.toggle(mail_a)
        assert manager._last_toggled == mail_a

        manager.toggle(mail_b)
        assert manager._last_toggled == mail_b

    def test_toggle_multiple_mails(self) -> None:
        """Multiple mails can be toggled independently."""
        manager = SelectionManager(uuid.uuid4())
        ids = [uuid.uuid4() for _ in range(5)]

        for mid in ids:
            manager.toggle(mid)

        assert manager.count == 5
        for mid in ids:
            assert mid in manager.selected_ids

    def test_toggle_returns_current_set(self) -> None:
        """toggle() returns the full current selection set."""
        manager = SelectionManager(uuid.uuid4())
        mail_a = uuid.uuid4()
        mail_b = uuid.uuid4()

        manager.toggle(mail_a)
        result = manager.toggle(mail_b)

        assert result == {mail_a, mail_b}


class TestSelectionManagerClear:
    """Tests for clear() method."""

    def test_clear_empties_selection(self) -> None:
        """clear() removes all selected mails."""
        manager = SelectionManager(uuid.uuid4())
        for _ in range(3):
            manager.toggle(uuid.uuid4())

        result = manager.clear()

        assert len(result) == 0
        assert manager.count == 0

    def test_clear_resets_last_toggled(self) -> None:
        """clear() sets _last_toggled to None."""
        manager = SelectionManager(uuid.uuid4())
        manager.toggle(uuid.uuid4())
        manager.clear()

        assert manager._last_toggled is None

    def test_clear_on_empty_is_noop(self) -> None:
        """clear() on empty selection does not raise."""
        manager = SelectionManager(uuid.uuid4())
        result = manager.clear()
        assert len(result) == 0

    def test_clear_returns_empty_set(self) -> None:
        """clear() returns an empty set."""
        manager = SelectionManager(uuid.uuid4())
        manager.toggle(uuid.uuid4())
        result = manager.clear()
        assert result == set()


class TestSelectionManagerProperties:
    """Tests for count and selected_ids properties."""

    def test_count_zero_initially(self) -> None:
        """Count is 0 when nothing is selected."""
        manager = SelectionManager(uuid.uuid4())
        assert manager.count == 0

    def test_count_reflects_selection_size(self) -> None:
        """Count matches the number of selected mails."""
        manager = SelectionManager(uuid.uuid4())
        for _ in range(7):
            manager.toggle(uuid.uuid4())
        assert manager.count == 7

    def test_selected_ids_returns_set(self) -> None:
        """selected_ids property returns a set."""
        manager = SelectionManager(uuid.uuid4())
        assert isinstance(manager.selected_ids, set)

    def test_account_id_stored(self) -> None:
        """Account ID is stored on the manager."""
        account_id = uuid.uuid4()
        manager = SelectionManager(account_id)
        assert manager.account_id == account_id


class TestSelectionManagerRegistry:
    """Tests for the global get_selection_manager registry."""

    def setup_method(self) -> None:
        """Clear the global registry between tests."""
        _selection_managers.clear()

    def test_creates_new_manager(self) -> None:
        """First call for an account creates a new manager."""
        account_id = uuid.uuid4()
        manager = get_selection_manager(account_id)

        assert isinstance(manager, SelectionManager)
        assert manager.account_id == account_id

    def test_returns_same_manager(self) -> None:
        """Subsequent calls for same account return the same manager."""
        account_id = uuid.uuid4()
        m1 = get_selection_manager(account_id)
        m2 = get_selection_manager(account_id)

        assert m1 is m2

    def test_different_accounts_get_different_managers(self) -> None:
        """Different accounts get independent managers."""
        acct_a = uuid.uuid4()
        acct_b = uuid.uuid4()
        m_a = get_selection_manager(acct_a)
        m_b = get_selection_manager(acct_b)

        assert m_a is not m_b

    def test_selection_state_persists(self) -> None:
        """Selection state persists across get_selection_manager calls."""
        account_id = uuid.uuid4()
        mail_id = uuid.uuid4()

        m1 = get_selection_manager(account_id)
        m1.toggle(mail_id)

        m2 = get_selection_manager(account_id)
        assert mail_id in m2.selected_ids
