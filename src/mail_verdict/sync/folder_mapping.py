"""
Folder mapping: auto-detect special-use folders and allow user override.

Maps logical folder types (inbox, junk, sent, drafts, trash, archive)
to actual IMAP folder names per account.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

FOLDER_TYPES = ("inbox", "junk", "sent", "drafts", "trash", "archive")

_NAME_PATTERNS: dict[str, list[str]] = {
    "inbox": ["INBOX"],
    "junk": ["Junk", "Junk Mail", "Spam", "Bulk Mail", "Junk E-mail"],
    "sent": ["Sent", "Sent Items", "Sent Messages", "Gesendet"],
    "drafts": ["Drafts", "Draft", "Entwuerfe"],
    "trash": ["Trash", "Deleted Items", "Deleted Messages", "Papierkorb", "Bin"],
    "archive": ["Archive", "Archives", "All Mail", "Archiv"],
}


def auto_detect_mapping(
    folders: list[dict[str, Any]],
) -> dict[str, str | None]:
    """
    Auto-detect folder mapping from a list of IMAP folders.

    Uses RFC 6154 SPECIAL-USE flags first, falls back to name matching.

    Args:
        folders: List of folder dicts with 'imap_name', 'special_use' keys

    Returns:
        Mapping of folder type to IMAP folder name
    """
    mapping: dict[str, str | None] = {ft: None for ft in FOLDER_TYPES}

    # Phase 1: match by special_use attribute
    for folder in folders:
        special = folder.get("special_use")
        if special and special in FOLDER_TYPES:
            mapping[special] = folder["imap_name"]

    # Phase 2: name-based fallback for unmapped types
    folder_names = {f["imap_name"]: f for f in folders}
    for folder_type, patterns in _NAME_PATTERNS.items():
        if mapping[folder_type] is not None:
            continue
        for pattern in patterns:
            if pattern in folder_names:
                mapping[folder_type] = pattern
                break
            # Case-insensitive match
            for name in folder_names:
                if name.lower() == pattern.lower():
                    mapping[folder_type] = name
                    break
            if mapping[folder_type] is not None:
                break

    return mapping


def get_mapped_folder(
    mapping: dict[str, Any] | None,
    folder_type: str,
    fallback: str | None = None,
) -> str | None:
    """
    Get the IMAP folder name for a logical folder type.

    Args:
        mapping: Account's folder_mapping JSONB
        folder_type: One of FOLDER_TYPES (inbox, junk, sent, etc.)
        fallback: Default if not mapped

    Returns:
        IMAP folder name or fallback
    """
    if mapping and folder_type in mapping:
        val = mapping[folder_type]
        return str(val) if val is not None else None
    return fallback
