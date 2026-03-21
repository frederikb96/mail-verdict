"""Tests for change detection: UID set parsing, UIDFlags dataclass."""

from __future__ import annotations

from mail_verdict.sync.change_detector import (
    UIDFlags,
    _parse_uid_set,
)


class TestParseUidSet:
    """Tests for _parse_uid_set (VANISHED response)."""

    def test_single_uid(self) -> None:
        """Parses a single UID."""
        result = _parse_uid_set("* VANISHED 42")
        assert result == [42]

    def test_uid_range(self) -> None:
        """Parses a UID range."""
        result = _parse_uid_set("* VANISHED 1:5")
        assert result == [1, 2, 3, 4, 5]

    def test_mixed_set(self) -> None:
        """Parses mixed single UIDs and ranges."""
        result = _parse_uid_set("* VANISHED 1:3,10,15:17")
        assert result == [1, 2, 3, 10, 15, 16, 17]

    def test_earlier_keyword(self) -> None:
        """Handles VANISHED (EARLIER) format."""
        result = _parse_uid_set("* VANISHED (EARLIER) 5:7")
        assert result == [5, 6, 7]

    def test_no_match(self) -> None:
        """Returns empty on non-VANISHED text."""
        result = _parse_uid_set("some random text")
        assert result == []


class TestUIDFlags:
    """Tests for UIDFlags dataclass."""

    def test_creation(self) -> None:
        """Can create UIDFlags with defaults."""
        uf = UIDFlags(uid=1, flags={"\\Seen"})
        assert uf.uid == 1
        assert uf.modseq is None

    def test_with_modseq(self) -> None:
        """Can create UIDFlags with modseq."""
        uf = UIDFlags(uid=1, flags=set(), modseq=42)
        assert uf.modseq == 42
