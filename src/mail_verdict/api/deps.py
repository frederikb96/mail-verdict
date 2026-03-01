"""
API dependency accessors.

Provides access to database repositories and services initialized during lifespan.
"""

from __future__ import annotations

from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.repository import (
    AttachmentRepository,
    FolderRepository,
    MailRepository,
    TagRepository,
    VerdictRepository,
)


def get_mail_repo() -> MailRepository:
    """Get MailRepository using the global DB connection."""
    return MailRepository(get_db_connection())


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
