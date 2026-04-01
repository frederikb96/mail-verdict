"""
API dependency accessors.

Provides access to database repositories and services initialized during lifespan.
"""

from __future__ import annotations

from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.repository import (
    AccountPrefsRepository,
    AttachmentRepository,
    FolderPrefsRepository,
    FolderRepository,
    MessageRepository,
    TagRepository,
    VerdictRepository,
)


def get_message_repo() -> MessageRepository:
    """Get MessageRepository using the global DB connection."""
    return MessageRepository(get_db_connection())


def get_verdict_repo() -> VerdictRepository:
    """Get VerdictRepository using the global DB connection."""
    return VerdictRepository(get_db_connection())


def get_folder_repo() -> FolderRepository:
    """Get FolderRepository using the global DB connection."""
    return FolderRepository(get_db_connection())


def get_attachment_repo() -> AttachmentRepository:
    """Get AttachmentRepository using the global DB connection."""
    return AttachmentRepository(get_db_connection())


def get_tag_repo() -> TagRepository:
    """Get TagRepository using the global DB connection."""
    return TagRepository(get_db_connection())


def get_account_prefs_repo() -> AccountPrefsRepository:
    """Get AccountPrefsRepository using the global DB connection."""
    return AccountPrefsRepository(get_db_connection())


def get_folder_prefs_repo() -> FolderPrefsRepository:
    """Get FolderPrefsRepository using the global DB connection."""
    return FolderPrefsRepository(get_db_connection())
