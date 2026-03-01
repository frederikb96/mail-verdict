"""MailVerdict database module."""

from mail_verdict.database.connection import (
    DatabaseConnection,
    close_database,
    get_db_connection,
    init_database,
)
from mail_verdict.database.models import (
    Account,
    Attachment,
    Base,
    Folder,
    Mail,
    MailTag,
    SpecialUse,
    TagSource,
    Verdict,
    VerdictSource,
)
from mail_verdict.database.repository import (
    AttachmentRepository,
    FolderRepository,
    MailRepository,
    TagRepository,
    VerdictRepository,
)

__all__ = [
    "Account",
    "Attachment",
    "AttachmentRepository",
    "Base",
    "DatabaseConnection",
    "Folder",
    "FolderRepository",
    "Mail",
    "MailRepository",
    "MailTag",
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
