"""Tests for folder mapping: auto-detection, name fallback, get_mapped_folder."""

from __future__ import annotations

from mail_verdict.sync.folder_mapping import (
    FOLDER_TYPES,
    auto_detect_mapping,
    get_mapped_folder,
)


class TestAutoDetectMapping:
    """Tests for auto_detect_mapping."""

    def test_detect_by_special_use(self) -> None:
        """Folders with special_use are mapped correctly."""
        folders = [
            {"imap_name": "INBOX", "special_use": "inbox"},
            {"imap_name": "Junk Mail", "special_use": "junk"},
            {"imap_name": "Sent Items", "special_use": "sent"},
        ]
        mapping = auto_detect_mapping(folders)
        assert mapping["inbox"] == "INBOX"
        assert mapping["junk"] == "Junk Mail"
        assert mapping["sent"] == "Sent Items"

    def test_name_fallback(self) -> None:
        """Folders without special_use are matched by name."""
        folders = [
            {"imap_name": "INBOX", "special_use": None},
            {"imap_name": "Spam", "special_use": None},
            {"imap_name": "Drafts", "special_use": None},
            {"imap_name": "Trash", "special_use": None},
        ]
        mapping = auto_detect_mapping(folders)
        assert mapping["inbox"] == "INBOX"
        assert mapping["junk"] == "Spam"
        assert mapping["drafts"] == "Drafts"
        assert mapping["trash"] == "Trash"

    def test_case_insensitive_fallback(self) -> None:
        """Name matching is case-insensitive."""
        folders = [{"imap_name": "inbox", "special_use": None}]
        mapping = auto_detect_mapping(folders)
        assert mapping["inbox"] == "inbox"

    def test_special_use_takes_precedence(self) -> None:
        """special_use overrides name matching."""
        folders = [
            {"imap_name": "MyCustomJunk", "special_use": "junk"},
            {"imap_name": "Junk", "special_use": None},
        ]
        mapping = auto_detect_mapping(folders)
        assert mapping["junk"] == "MyCustomJunk"

    def test_unmapped_types_are_none(self) -> None:
        """Unmapped folder types return None."""
        folders = [{"imap_name": "INBOX", "special_use": "inbox"}]
        mapping = auto_detect_mapping(folders)
        assert mapping["archive"] is None

    def test_empty_folders(self) -> None:
        """Empty folder list returns all None."""
        mapping = auto_detect_mapping([])
        for ft in FOLDER_TYPES:
            assert mapping[ft] is None

    def test_all_types_present(self) -> None:
        """All FOLDER_TYPES are present in result."""
        mapping = auto_detect_mapping([])
        for ft in FOLDER_TYPES:
            assert ft in mapping


class TestGetMappedFolder:
    """Tests for get_mapped_folder."""

    def test_returns_mapped_value(self) -> None:
        """Returns the mapped folder name."""
        mapping = {"junk": "My Junk Folder"}
        assert get_mapped_folder(mapping, "junk") == "My Junk Folder"

    def test_returns_fallback_when_not_mapped(self) -> None:
        """Returns fallback when type not in mapping."""
        mapping = {"junk": "Junk"}
        assert get_mapped_folder(mapping, "archive", fallback="Archive") == "Archive"

    def test_returns_none_when_no_mapping(self) -> None:
        """Returns None when mapping is None."""
        assert get_mapped_folder(None, "junk") is None

    def test_returns_fallback_when_none_mapping(self) -> None:
        """Returns fallback when mapping is None."""
        assert get_mapped_folder(None, "junk", fallback="Junk") == "Junk"
