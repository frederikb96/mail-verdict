"""Tests for IMAP extension layer: SELECT parsing, LIST parsing, capabilities."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mail_verdict.sync.extensions import (
    SPECIAL_USE_FLAGS,
    AsyncIMAPExtended,
    FolderInfo,
    SelectResult,
    _parse_list_response,
    _parse_select_response,
    _quote_mailbox,
)


class TestQuoteMailbox:
    """Tests for _quote_mailbox."""

    def test_plain_name(self) -> None:
        """Plain name is returned as-is."""
        assert _quote_mailbox("INBOX") == "INBOX"

    def test_name_with_spaces(self) -> None:
        """Name with spaces is quoted."""
        assert _quote_mailbox("My Folder") == '"My Folder"'


class TestParseSelectResponse:
    """Tests for _parse_select_response."""

    def _make_response(self, result: str = "OK", lines: list[bytes] | None = None) -> MagicMock:
        """Create a mock IMAP response."""
        resp = MagicMock()
        resp.result = result
        resp.lines = lines or []
        return resp

    def test_ok_response(self) -> None:
        """Parses OK SELECT response."""
        resp = self._make_response("OK", [
            b"* 42 EXISTS",
            b"* 3 RECENT",
            b"* OK [UIDVALIDITY 12345]",
            b"* OK [UIDNEXT 100]",
        ])
        result = _parse_select_response(resp)
        assert result.ok is True
        assert result.exists == 42
        assert result.recent == 3
        assert result.uidvalidity == 12345
        assert result.uidnext == 100

    def test_highestmodseq(self) -> None:
        """Parses HIGHESTMODSEQ from CONDSTORE response."""
        resp = self._make_response("OK", [
            b"* OK [HIGHESTMODSEQ 9999]",
        ])
        result = _parse_select_response(resp)
        assert result.highestmodseq == 9999

    def test_flags_parsed(self) -> None:
        """Parses FLAGS from SELECT response."""
        resp = self._make_response("OK", [
            b"* FLAGS (\\Answered \\Flagged \\Seen)",
        ])
        result = _parse_select_response(resp)
        assert "\\Answered" in result.flags

    def test_permanent_flags_parsed(self) -> None:
        """Parses PERMANENTFLAGS from SELECT response."""
        resp = self._make_response("OK", [
            b"* OK [PERMANENTFLAGS (\\Answered \\Flagged \\Seen \\*)]",
        ])
        result = _parse_select_response(resp)
        assert "\\Answered" in result.permanent_flags

    def test_failed_response(self) -> None:
        """NO response sets ok=False."""
        resp = self._make_response("NO", [])
        result = _parse_select_response(resp)
        assert result.ok is False


class TestParseListResponse:
    """Tests for _parse_list_response."""

    def test_basic_folders(self) -> None:
        """Parses standard LIST response lines."""
        resp = MagicMock()
        resp.result = "OK"
        resp.lines = [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren) "/" "Sent"',
        ]
        folders = _parse_list_response(resp)
        assert len(folders) == 2
        assert folders[0].name == "INBOX"
        assert folders[1].name == "Sent"

    def test_special_use_flags(self) -> None:
        """Detects SPECIAL-USE flags."""
        resp = MagicMock()
        resp.result = "OK"
        resp.lines = [
            b'(\\HasNoChildren \\Junk) "/" "Junk"',
            b'(\\HasNoChildren \\Trash) "/" "Trash"',
            b'(\\HasNoChildren \\Sent) "/" "Sent"',
        ]
        folders = _parse_list_response(resp)
        assert folders[0].special_use == "junk"
        assert folders[1].special_use == "trash"
        assert folders[2].special_use == "sent"

    def test_non_bytes_skipped(self) -> None:
        """Non-bytes lines are skipped."""
        resp = MagicMock()
        resp.result = "OK"
        resp.lines = ["not bytes", b'(\\HasNoChildren) "/" "INBOX"']
        folders = _parse_list_response(resp)
        assert len(folders) == 1

    def test_with_star_list_prefix(self) -> None:
        """Handles lines with '* LIST ' prefix."""
        resp = MagicMock()
        resp.result = "OK"
        resp.lines = [
            b'* LIST (\\HasNoChildren) "/" "Archive"',
        ]
        folders = _parse_list_response(resp)
        assert len(folders) == 1
        assert folders[0].name == "Archive"


class TestAsyncIMAPExtended:
    """Tests for AsyncIMAPExtended wrapper."""

    def _make_extended(self, capabilities: set[str] | None = None) -> AsyncIMAPExtended:
        """Create an AsyncIMAPExtended with a mock client."""
        client = MagicMock()
        client.protocol = MagicMock()
        client.protocol.capabilities = capabilities or {"IMAP4rev1", "CONDSTORE"}
        return AsyncIMAPExtended(client)

    def test_has_capability(self) -> None:
        """has_capability checks protocol capabilities."""
        ext = self._make_extended({"CONDSTORE", "IDLE"})
        assert ext.has_capability("CONDSTORE") is True
        assert ext.has_capability("QRESYNC") is False

    def test_capabilities_property(self) -> None:
        """capabilities returns the set from protocol."""
        ext = self._make_extended({"A", "B"})
        assert ext.capabilities == {"A", "B"}


class TestSpecialUseFlags:
    """Tests for SPECIAL_USE_FLAGS mapping."""

    @pytest.mark.parametrize("flag,expected", [
        ("\\Junk", "junk"),
        ("\\Trash", "trash"),
        ("\\Sent", "sent"),
        ("\\Drafts", "drafts"),
        ("\\Archive", "archive"),
        ("\\All", "all"),
        ("\\Flagged", "flagged"),
    ])
    def test_flag_mapping(self, flag: str, expected: str) -> None:
        """All RFC 6154 flags map correctly."""
        assert SPECIAL_USE_FLAGS[flag] == expected
