"""Tests for SyncManager: event classification, sync orchestration."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from mail_verdict.database.models import SpecialUse
from mail_verdict.sync.events import MailReceived, MailSpamDetected, MailTrashed
from mail_verdict.sync.manager import SyncManager


def _make_folder(
    special_use: SpecialUse | None = None,
    imap_name: str = "INBOX",
) -> MagicMock:
    """Create a mock Folder."""
    folder = MagicMock()
    folder.id = uuid.uuid4()
    folder.imap_name = imap_name
    folder.special_use = special_use
    folder.uidvalidity = 1
    folder.uidnext = 100
    folder.highestmodseq = 50
    return folder


def _make_manager() -> SyncManager:
    """Create a SyncManager with all-mock dependencies."""
    account = MagicMock()
    account.name = "test"
    config = MagicMock()
    config.sync.poll_interval_seconds = 1
    config.sync.auto_detect_folders = False

    return SyncManager(
        account=account,
        account_id=uuid.uuid4(),
        connector=MagicMock(),
        folder_repo=MagicMock(),
        mail_repo=MagicMock(),
        attachment_repo=MagicMock(),
        config=config,
    )


class TestClassifyFolderEvents:
    """Tests for _classify_folder_events."""

    def test_trash_folder_reclassifies(self) -> None:
        """MailReceived in trash becomes MailTrashed."""
        manager = _make_manager()
        folder = _make_folder(special_use=SpecialUse.TRASH)
        account_id = uuid.uuid4()

        events = [
            MailReceived(account_id=account_id, folder_id=folder.id, uid=1),
        ]
        classified = manager._classify_folder_events(events, folder)
        assert len(classified) == 1
        assert isinstance(classified[0], MailTrashed)

    def test_junk_folder_reclassifies(self) -> None:
        """MailReceived in junk becomes MailSpamDetected."""
        manager = _make_manager()
        folder = _make_folder(special_use=SpecialUse.JUNK)
        account_id = uuid.uuid4()

        events = [
            MailReceived(account_id=account_id, folder_id=folder.id, uid=1),
        ]
        classified = manager._classify_folder_events(events, folder)
        assert len(classified) == 1
        assert isinstance(classified[0], MailSpamDetected)

    def test_inbox_unchanged(self) -> None:
        """MailReceived in inbox stays MailReceived."""
        manager = _make_manager()
        folder = _make_folder(special_use=SpecialUse.INBOX)
        account_id = uuid.uuid4()

        events = [
            MailReceived(account_id=account_id, folder_id=folder.id, uid=1),
        ]
        classified = manager._classify_folder_events(events, folder)
        assert len(classified) == 1
        assert isinstance(classified[0], MailReceived)

    def test_no_special_use_unchanged(self) -> None:
        """Folder without special_use leaves events unchanged."""
        manager = _make_manager()
        folder = _make_folder(special_use=None)
        account_id = uuid.uuid4()

        events = [
            MailReceived(account_id=account_id, folder_id=folder.id, uid=1),
        ]
        classified = manager._classify_folder_events(events, folder)
        assert isinstance(classified[0], MailReceived)

    def test_empty_events(self) -> None:
        """Empty event list returns empty."""
        manager = _make_manager()
        folder = _make_folder(special_use=SpecialUse.TRASH)
        assert manager._classify_folder_events([], folder) == []


class TestSyncManagerLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_sets_running(self) -> None:
        """start() sets _running flag."""
        manager = _make_manager()
        await manager.start()
        assert manager._running is True
        await manager.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_running(self) -> None:
        """stop() clears _running flag."""
        manager = _make_manager()
        await manager.start()
        await manager.stop()
        assert manager._running is False

    @pytest.mark.asyncio
    async def test_double_start_is_noop(self) -> None:
        """Starting twice does not create duplicate tasks."""
        manager = _make_manager()
        await manager.start()
        task1 = manager._task
        await manager.start()
        assert manager._task is task1
        await manager.stop()
