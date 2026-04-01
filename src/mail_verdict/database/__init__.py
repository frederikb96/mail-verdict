"""MailVerdict database module."""

from mail_verdict.database.connection import (
    DatabaseConnection,
    close_database,
    get_db_connection,
    init_database,
)
from mail_verdict.database.models import (
    Account,
    AccountPrefs,
    Attachment,
    Base,
    Folder,
    FolderPrefs,
    MailTag,
    Message,
    SpecialUse,
    TagSource,
    Verdict,
    VerdictSource,
)
from mail_verdict.database.repository import (
    AccountPrefsRepository,
    AccountRepository,
    AttachmentRepository,
    FolderPrefsRepository,
    FolderRepository,
    MessageRepository,
    TagRepository,
    VerdictRepository,
)

__all__ = [
    "Account",
    "AccountPrefs",
    "AccountPrefsRepository",
    "AccountRepository",
    "Attachment",
    "AttachmentRepository",
    "Base",
    "DatabaseConnection",
    "Folder",
    "FolderPrefs",
    "FolderPrefsRepository",
    "FolderRepository",
    "MailTag",
    "Message",
    "MessageRepository",
    "SpecialUse",
    "TagRepository",
    "TagSource",
    "Verdict",
    "VerdictRepository",
    "VerdictSource",
    "close_database",
    "get_db_connection",
    "init_database",
]
