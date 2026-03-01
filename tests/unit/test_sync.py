"""
Unit tests for IMAP sync logic with mocked aioimaplib.

Row 150 (o=10.42): capability detection, folder discovery,
change detection (all 3 tiers), UIDVALIDITY guard.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.sync.change_detector import (
    ChangeDetector,
    ChangeSet,
    _parse_fetch_flags,
    _parse_uid_set,
)
from mail_verdict.sync.extensions import (
    FolderInfo,
    SelectResult,
    _parse_list_response,
    _parse_select_response,
)
from mail_verdict.sync.folders import detect_special_use

pytestmark = pytest.mark.unit


# ===========================================================================
# Extension layer tests
# ===========================================================================


class TestCapabilityDetection:
    """Tests for IMAP capability detection."""

    def test_has_capability_true(self, mock_imap_extended: MagicMock) -> None:
        """Returns True for capabilities in the set."""
        assert mock_imap_extended.has_capability("CONDSTORE") is True

    def test_has_capability_false(self, mock_imap_extended: MagicMock) -> None:
        """Returns False for capabilities not in the set."""
        assert mock_imap_extended.has_capability("QRESYNC") is False


class TestSelectParsing:
    """Tests for SELECT response parsing."""

    def test_parse_basic_select(self) -> None:
        """Parse a minimal SELECT response."""
        response = MagicMock()
        response.result = "OK"
        response.lines = [
            b"* 42 EXISTS",
            b"* 0 RECENT",
            b"* OK [UIDVALIDITY 12345]",
            b"* OK [UIDNEXT 100]",
            b"* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)",
        ]

        result = _parse_select_response(response)
        assert result.ok is True
        assert result.exists == 42
        assert result.recent == 0
        assert result.uidvalidity == 12345
        assert result.uidnext == 100

    def test_parse_condstore_select(self) -> None:
        """Parse SELECT response with HIGHESTMODSEQ."""
        response = MagicMock()
        response.result = "OK"
        response.lines = [
            b"* 42 EXISTS",
            b"* OK [UIDVALIDITY 12345]",
            b"* OK [UIDNEXT 100]",
            b"* OK [HIGHESTMODSEQ 98765]",
        ]

        result = _parse_select_response(response)
        assert result.highestmodseq == 98765

    def test_parse_failed_select(self) -> None:
        """Parse a failed SELECT response."""
        response = MagicMock()
        response.result = "NO"
        response.lines = []

        result = _parse_select_response(response)
        assert result.ok is False


class TestListParsing:
    """Tests for LIST response parsing."""

    def test_parse_basic_list(self) -> None:
        """Parse a standard LIST response."""
        response = MagicMock()
        response.result = "OK"
        response.lines = [
            b'* LIST (\\HasNoChildren) "/" "INBOX"',
            b'* LIST (\\HasNoChildren \\Sent) "/" "Sent"',
            b'* LIST (\\HasNoChildren \\Trash) "/" "Trash"',
        ]

        folders = _parse_list_response(response)
        assert len(folders) == 3
        assert folders[0].name == "INBOX"
        assert folders[1].special_use == "sent"
        assert folders[2].special_use == "trash"


class TestFolderDiscovery:
    """Tests for special-use folder detection."""

    def test_detect_from_rfc6154_flag(self) -> None:
        """RFC 6154 flags are used first."""
        folder = FolderInfo(name="Outbox", separator="/", flags=["\\Sent"], special_use="sent")
        assert detect_special_use(folder) == "sent"

    def test_detect_from_name_fallback(self) -> None:
        """Falls back to name-based detection."""
        folder = FolderInfo(name="Trash", separator="/", flags=[], special_use=None)
        assert detect_special_use(folder) == "trash"

    def test_detect_inbox(self) -> None:
        """INBOX detected by name."""
        folder = FolderInfo(name="INBOX", separator="/", flags=[], special_use=None)
        assert detect_special_use(folder) == "inbox"

    def test_detect_german_names(self) -> None:
        """German folder names are detected correctly."""
        folder = FolderInfo(name="Papierkorb", separator="/", flags=[], special_use=None)
        assert detect_special_use(folder) == "trash"

        folder2 = FolderInfo(name="Gesendet", separator="/", flags=[], special_use=None)
        assert detect_special_use(folder2) == "sent"

    def test_unknown_folder(self) -> None:
        """Unknown folder returns None."""
        folder = FolderInfo(name="Custom Folder", separator="/", flags=[], special_use=None)
        assert detect_special_use(folder) is None


# ===========================================================================
# Change detection tests
# ===========================================================================


class TestFetchFlagsParsing:
    """Tests for _parse_fetch_flags utility."""

    def test_parse_basic_flags(self) -> None:
        """Parse UID FETCH FLAGS response."""
        lines = [
            b"1 FETCH (UID 42 FLAGS (\\Seen \\Flagged))",
            b"2 FETCH (UID 43 FLAGS (\\Seen))",
        ]
        results = _parse_fetch_flags(lines)
        assert len(results) == 2
        assert results[0].uid == 42
        assert "\\Seen" in results[0].flags
        assert "\\Flagged" in results[0].flags
        assert results[1].uid == 43

    def test_parse_flags_with_modseq(self) -> None:
        """Parse flags with MODSEQ extension."""
        lines = [
            b"1 FETCH (UID 42 FLAGS (\\Seen) MODSEQ (12345))",
        ]
        results = _parse_fetch_flags(lines)
        assert len(results) == 1
        assert results[0].modseq == 12345

    def test_ignore_non_bytes(self) -> None:
        """Non-bytes lines are skipped."""
        lines = ["not bytes"]  # type: ignore[list-item]
        results = _parse_fetch_flags(lines)
        assert results == []


class TestParseUIDSet:
    """Tests for _parse_uid_set (VANISHED response)."""

    def test_simple_uids(self) -> None:
        """Parse simple UID list."""
        result = _parse_uid_set("* OK VANISHED 1,3,5")
        assert result == [1, 3, 5]

    def test_uid_ranges(self) -> None:
        """Parse UID ranges."""
        result = _parse_uid_set("* OK VANISHED 1:3,7")
        assert result == [1, 2, 3, 7]

    def test_vanished_earlier(self) -> None:
        """Parse VANISHED (EARLIER) response."""
        result = _parse_uid_set("* OK VANISHED (EARLIER) 10:12,15")
        assert result == [10, 11, 12, 15]

    def test_no_match(self) -> None:
        """Returns empty list for non-VANISHED text."""
        result = _parse_uid_set("* OK some other response")
        assert result == []


class TestChangeDetectorTiers:
    """Tests for ChangeDetector tier selection and change detection."""

    @pytest.fixture
    def mock_folder_repo(self) -> AsyncMock:
        """Mock FolderRepository."""
        repo = AsyncMock()
        repo.get_by_account = AsyncMock(return_value=[])
        repo.upsert_folder = AsyncMock()
        repo.get_state = AsyncMock(return_value=None)
        return repo

    @pytest.fixture
    def mock_mail_repo(self) -> AsyncMock:
        """Mock MailRepository."""
        repo = AsyncMock()
        repo.get_by_folder = AsyncMock(return_value=[])
        return repo

    @pytest.fixture
    def detector(self, mock_folder_repo: AsyncMock, mock_mail_repo: AsyncMock) -> ChangeDetector:
        """Create a ChangeDetector with mocked repos."""
        return ChangeDetector(
            folder_repo=mock_folder_repo,
            mail_repo=mock_mail_repo,
        )

    @pytest.fixture
    def mock_folder(self) -> MagicMock:
        """Mock Folder ORM object."""
        folder = MagicMock()
        folder.id = uuid.UUID("00000000-0000-0000-0000-000000000010")
        folder.imap_name = "INBOX"
        folder.uidvalidity = 1234
        folder.uidnext = 50
        folder.highestmodseq = 5000
        return folder

    @pytest.mark.asyncio(loop_scope="function")
    async def test_uidvalidity_change_triggers_full_resync(
        self,
        detector: ChangeDetector,
        mock_imap_extended: MagicMock,
        mock_folder: MagicMock,
    ) -> None:
        """UIDVALIDITY change forces full resync regardless of capabilities."""
        # Server returns different UIDVALIDITY
        select_result = SelectResult(
            ok=True,
            uidvalidity=9999,
            uidnext=100,
            highestmodseq=6000,
        )
        mock_imap_extended.select_condstore = AsyncMock(return_value=select_result)

        # Mock the full diff methods
        mock_response = MagicMock()
        mock_response.result = "OK"
        mock_response.lines = []
        mock_imap_extended.client.uid = AsyncMock(return_value=mock_response)

        account_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        changeset, events = await detector.detect_changes(
            mock_imap_extended,
            account_id,
            mock_folder,
        )
        assert changeset.uidvalidity_changed is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_tier3_full_diff_on_no_modseq(
        self,
        detector: ChangeDetector,
        mock_imap_extended: MagicMock,
        mock_folder: MagicMock,
    ) -> None:
        """Falls back to full diff when no stored modseq."""
        mock_folder.highestmodseq = None
        mock_folder.uidvalidity = 1234

        select_result = SelectResult(
            ok=True,
            uidvalidity=1234,
            uidnext=100,
            highestmodseq=None,
        )
        mock_imap_extended.has_capability = MagicMock(return_value=False)
        mock_imap_extended.select_plain = AsyncMock(return_value=select_result)

        mock_response = MagicMock()
        mock_response.result = "OK"
        mock_response.lines = []
        mock_imap_extended.client.uid = AsyncMock(return_value=mock_response)

        account_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        changeset, events = await detector.detect_changes(
            mock_imap_extended,
            account_id,
            mock_folder,
        )
        assert isinstance(changeset, ChangeSet)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_select_failure_returns_empty(
        self,
        detector: ChangeDetector,
        mock_imap_extended: MagicMock,
        mock_folder: MagicMock,
    ) -> None:
        """Failed SELECT returns empty changeset."""
        mock_folder.highestmodseq = None
        mock_folder.uidvalidity = None

        select_result = SelectResult(ok=False)
        mock_imap_extended.has_capability = MagicMock(return_value=False)
        mock_imap_extended.select_plain = AsyncMock(return_value=select_result)

        account_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        changeset, events = await detector.detect_changes(
            mock_imap_extended,
            account_id,
            mock_folder,
        )
        assert changeset.new_uids == []
        assert changeset.deleted_uids == []
        assert events == []
