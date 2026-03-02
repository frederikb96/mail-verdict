"""Tests for change detection: UID diff, VANISHED parsing, flag parsing."""

from __future__ import annotations

from mail_verdict.sync.change_detector import (
    UIDFlags,
    _parse_fetch_flags,
    _parse_uid_set,
)


class TestParseFetchFlags:
    """Tests for _parse_fetch_flags."""

    def test_basic_uid_flags(self) -> None:
        """Parses UID and FLAGS from FETCH response."""
        lines = [b"1 FETCH (UID 42 FLAGS (\\Seen \\Flagged))"]
        result = _parse_fetch_flags(lines)
        assert len(result) == 1
        assert result[0].uid == 42
        assert "\\Seen" in result[0].flags
        assert "\\Flagged" in result[0].flags

    def test_with_modseq(self) -> None:
        """Parses MODSEQ from FETCH response."""
        lines = [b"1 FETCH (UID 10 FLAGS (\\Seen) MODSEQ (99))"]
        result = _parse_fetch_flags(lines)
        assert len(result) == 1
        assert result[0].uid == 10
        assert result[0].modseq == 99

    def test_empty_flags(self) -> None:
        """Handles empty flags set."""
        lines = [b"1 FETCH (UID 5 FLAGS ())"]
        result = _parse_fetch_flags(lines)
        assert len(result) == 1
        assert result[0].flags == set()

    def test_multiple_messages(self) -> None:
        """Parses multiple FETCH response lines."""
        lines = [
            b"1 FETCH (UID 1 FLAGS (\\Seen))",
            b"2 FETCH (UID 2 FLAGS ())",
            b"3 FETCH (UID 3 FLAGS (\\Flagged))",
        ]
        result = _parse_fetch_flags(lines)
        assert len(result) == 3
        assert [r.uid for r in result] == [1, 2, 3]

    def test_non_bytes_skipped(self) -> None:
        """Non-bytes lines are ignored."""
        lines = ["not bytes", b"1 FETCH (UID 1 FLAGS (\\Seen))"]  # type: ignore[list-item]
        result = _parse_fetch_flags(lines)
        assert len(result) == 1

    def test_non_matching_lines_skipped(self) -> None:
        """Lines not matching FETCH pattern are skipped."""
        lines = [b"* OK FETCH completed", b"garbage"]
        result = _parse_fetch_flags(lines)
        assert len(result) == 0


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
