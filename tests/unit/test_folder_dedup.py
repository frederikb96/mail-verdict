"""Tests for folder discovery: special-use detection, dedup, name fallback."""

from __future__ import annotations

from mail_verdict.sync.folders import (
    SPECIAL_USE_FLAGS,
    FolderInfo,
    _parse_imap_tools_folder,
    detect_special_use,
)


class TestDetectSpecialUse:
    """Tests for detect_special_use()."""

    def test_rfc6154_flag_wins(self) -> None:
        """Folder with RFC 6154 flag returns that type."""
        folder = FolderInfo(name="MyCustomInbox", separator="/", special_use="inbox")
        assert detect_special_use(folder) == "inbox"

    def test_inbox_by_name(self) -> None:
        """INBOX is detected by name when no flag."""
        folder = FolderInfo(name="INBOX", separator="/")
        assert detect_special_use(folder) == "inbox"

    def test_inbox_case_insensitive(self) -> None:
        """Inbox name detection is case-insensitive."""
        folder = FolderInfo(name="Inbox", separator="/")
        assert detect_special_use(folder) == "inbox"

    def test_sent_by_name(self) -> None:
        """Sent Items is detected by name fallback."""
        folder = FolderInfo(name="Sent Items", separator="/")
        assert detect_special_use(folder) == "sent"

    def test_junk_by_name(self) -> None:
        """Spam folder is detected as junk."""
        folder = FolderInfo(name="Spam", separator="/")
        assert detect_special_use(folder) == "junk"

    def test_drafts_by_name(self) -> None:
        """Drafts folder is detected."""
        folder = FolderInfo(name="Drafts", separator="/")
        assert detect_special_use(folder) == "drafts"

    def test_trash_by_name(self) -> None:
        """Deleted Items is detected as trash."""
        folder = FolderInfo(name="Deleted Items", separator="/")
        assert detect_special_use(folder) == "trash"

    def test_archive_by_name(self) -> None:
        """Archive folder is detected."""
        folder = FolderInfo(name="Archive", separator="/")
        assert detect_special_use(folder) == "archive"

    def test_all_mail_is_archive(self) -> None:
        """All Mail maps to archive."""
        folder = FolderInfo(name="All Mail", separator="/")
        assert detect_special_use(folder) == "archive"

    def test_german_trash_name(self) -> None:
        """Papierkorb (German trash) is detected."""
        folder = FolderInfo(name="Papierkorb", separator="/")
        assert detect_special_use(folder) == "trash"

    def test_german_junk_name(self) -> None:
        """German junk folder name is detected."""
        folder = FolderInfo(name="Junk-E-Mail", separator="/")
        assert detect_special_use(folder) == "junk"

    def test_unknown_folder_returns_none(self) -> None:
        """Custom folder with no match returns None."""
        folder = FolderInfo(name="MyCustomFolder", separator="/")
        assert detect_special_use(folder) is None

    def test_flag_takes_precedence_over_name(self) -> None:
        """RFC 6154 flag takes precedence over name-based detection."""
        folder = FolderInfo(
            name="Spam",
            separator="/",
            special_use="sent",
        )
        assert detect_special_use(folder) == "sent"


class TestParseImapToolsFolder:
    """Tests for _parse_imap_tools_folder()."""

    def test_basic_folder(self) -> None:
        """Parses a basic folder with no special-use flags."""
        from unittest.mock import MagicMock

        fi = MagicMock()
        fi.name = "INBOX"
        fi.delim = "/"
        fi.flags = ["\\HasNoChildren"]

        result = _parse_imap_tools_folder(fi)

        assert result.name == "INBOX"
        assert result.separator == "/"
        assert result.special_use is None

    def test_folder_with_junk_flag(self) -> None:
        """Parses folder with \\Junk flag."""
        from unittest.mock import MagicMock

        fi = MagicMock()
        fi.name = "Junk Email"
        fi.delim = "."
        fi.flags = ["\\HasNoChildren", "\\Junk"]

        result = _parse_imap_tools_folder(fi)

        assert result.name == "Junk Email"
        assert result.special_use == "junk"

    def test_folder_with_archive_flag(self) -> None:
        """Parses folder with \\Archive flag."""
        from unittest.mock import MagicMock

        fi = MagicMock()
        fi.name = "All Mail"
        fi.delim = "/"
        fi.flags = ["\\Archive", "\\HasNoChildren"]

        result = _parse_imap_tools_folder(fi)

        assert result.special_use == "archive"

    def test_folder_with_no_flags(self) -> None:
        """Parses folder with empty/None flags."""
        from unittest.mock import MagicMock

        fi = MagicMock()
        fi.name = "Custom"
        fi.delim = "/"
        fi.flags = None

        result = _parse_imap_tools_folder(fi)

        assert result.flags == []
        assert result.special_use is None


class TestSpecialUseFlagsMapping:
    """Tests for the SPECIAL_USE_FLAGS constant."""

    def test_all_rfc6154_flags_present(self) -> None:
        """All RFC 6154 flags are mapped."""
        expected_flags = [
            "\\All", "\\Archive", "\\Drafts",
            "\\Flagged", "\\Junk", "\\Sent", "\\Trash",
        ]
        for flag in expected_flags:
            assert flag in SPECIAL_USE_FLAGS

    def test_flags_map_to_lowercase(self) -> None:
        """All flag values are lowercase."""
        for value in SPECIAL_USE_FLAGS.values():
            assert value == value.lower()


class TestFolderDedup:
    """Tests for special-use dedup logic (RFC 6154 flag wins)."""

    def test_two_junk_folders_rfc_wins(self) -> None:
        """When two folders both detect as junk, the one with RFC flag wins."""
        # Simulate: "Junk Email" has \\Junk flag, "Spam" matches by name
        flagged = FolderInfo(
            name="Junk Email", separator="/", special_use="junk",
        )
        name_matched = FolderInfo(
            name="Spam", separator="/", special_use=None,
        )

        # Replicate the dedup logic from discover_folders
        seen: dict[str, FolderInfo] = {}
        folders = [name_matched, flagged]  # Name match seen first

        for fi in folders:
            su = detect_special_use(fi)
            if su and su in seen:
                existing = seen[su]
                if fi.special_use and not existing.special_use:
                    seen[su] = fi
            elif su:
                seen[su] = fi

        assert seen["junk"] is flagged

    def test_two_junk_folders_first_with_flag_kept(self) -> None:
        """When first folder has RFC flag and second only name match, first stays."""
        flagged = FolderInfo(
            name="Junk Email", separator="/", special_use="junk",
        )
        name_matched = FolderInfo(
            name="Spam", separator="/", special_use=None,
        )

        seen: dict[str, FolderInfo] = {}
        folders = [flagged, name_matched]

        for fi in folders:
            su = detect_special_use(fi)
            if su and su in seen:
                existing = seen[su]
                if fi.special_use and not existing.special_use:
                    seen[su] = fi
            elif su:
                seen[su] = fi

        assert seen["junk"] is flagged

    def test_two_name_matched_first_wins(self) -> None:
        """When both folders match by name only, first one wins."""
        spam1 = FolderInfo(name="Spam", separator="/", special_use=None)
        spam2 = FolderInfo(name="Bulk Mail", separator="/", special_use=None)

        seen: dict[str, FolderInfo] = {}
        folders = [spam1, spam2]

        for fi in folders:
            su = detect_special_use(fi)
            if su and su in seen:
                existing = seen[su]
                if fi.special_use and not existing.special_use:
                    seen[su] = fi
            elif su:
                seen[su] = fi

        assert seen["junk"] is spam1

    def test_unique_types_no_conflict(self) -> None:
        """Different special-use types produce no dedup conflicts."""
        inbox = FolderInfo(name="INBOX", separator="/")
        sent = FolderInfo(name="Sent", separator="/")
        trash = FolderInfo(name="Trash", separator="/")

        seen: dict[str, FolderInfo] = {}
        for fi in [inbox, sent, trash]:
            su = detect_special_use(fi)
            if su and su not in seen:
                seen[su] = fi

        assert len(seen) == 3
        assert "inbox" in seen
        assert "sent" in seen
        assert "trash" in seen


class TestNameFallbackCoverage:
    """Tests for name fallback mappings."""

    def test_all_fallback_values_are_valid_types(self) -> None:
        """All name fallback values map to known types."""
        from mail_verdict.sync.folders import _NAME_FALLBACK

        valid_types = {"inbox", "sent", "drafts", "trash", "junk", "archive"}
        for value in _NAME_FALLBACK.values():
            assert value in valid_types, f"{value} is not a valid type"

    def test_junk_mail_variants(self) -> None:
        """Multiple junk/spam name variants are handled."""
        junk_names = ["junk", "junk mail", "spam", "bulk mail", "junk e-mail"]
        for name in junk_names:
            folder = FolderInfo(name=name, separator="/")
            result = detect_special_use(folder)
            assert result == "junk", f"{name} should map to junk, got {result}"

    def test_trash_variants(self) -> None:
        """Multiple trash name variants are handled."""
        trash_names = ["trash", "deleted items", "deleted messages", "bin"]
        for name in trash_names:
            folder = FolderInfo(name=name, separator="/")
            result = detect_special_use(folder)
            assert result == "trash", f"{name} should map to trash, got {result}"
