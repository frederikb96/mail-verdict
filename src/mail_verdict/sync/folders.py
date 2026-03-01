"""
Folder discovery and type detection.

Discovers IMAP folders, auto-detects types via RFC 6154 SPECIAL-USE flags
with case-insensitive name fallback, and syncs against stored folder state.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from mail_verdict.sync.extensions import AsyncIMAPExtended, FolderInfo

if TYPE_CHECKING:
    from mail_verdict.database.repository import FolderRepository

logger = logging.getLogger(__name__)

# Case-insensitive folder name patterns for special-use detection fallback.
# Maps lowercased folder names to SpecialUse enum values.
_NAME_FALLBACK: dict[str, str] = {
    # inbox
    "inbox": "inbox",
    # sent
    "sent": "sent",
    "sent items": "sent",
    "sent messages": "sent",
    "gesendet": "sent",
    "envoyés": "sent",
    "envoy&AOk-s": "sent",
    "elementos enviados": "sent",
    # drafts
    "drafts": "drafts",
    "draft": "drafts",
    "entwürfe": "drafts",
    "entw&APw-rfe": "drafts",
    "brouillons": "drafts",
    "borradores": "drafts",
    # trash
    "trash": "trash",
    "deleted items": "trash",
    "deleted messages": "trash",
    "papierkorb": "trash",
    "corbeille": "trash",
    "eliminados": "trash",
    "bin": "trash",
    # junk/spam
    "junk": "junk",
    "junk e-mail": "junk",
    "spam": "junk",
    "bulk mail": "junk",
    "junk-e-mail": "junk",
    "indésirables": "junk",
    "unerwünscht": "junk",
    "correo no deseado": "junk",
    # archive
    "archive": "archive",
    "archives": "archive",
    "archiv": "archive",
    "all mail": "archive",
    "all": "archive",
}


def detect_special_use(folder: FolderInfo) -> str | None:
    """
    Detect special-use type for a folder.

    Strategy: RFC 6154 flags first, then case-insensitive name matching.

    Args:
        folder: Parsed folder info from LIST response
    """
    if folder.special_use:
        return folder.special_use

    name_lower = folder.name.lower().strip()

    if name_lower == "inbox":
        return "inbox"

    return _NAME_FALLBACK.get(name_lower)


async def discover_folders(
    extended: AsyncIMAPExtended,
    account_id: uuid.UUID,
    folder_repo: FolderRepository,
    *,
    auto_detect: bool = True,
) -> list[uuid.UUID]:
    """
    Discover IMAP folders and sync with database.

    New folders on server get created in DB. Folders missing from server
    are left in DB (mails preserved) but could be marked via subscribed=False.

    Args:
        extended: Authenticated IMAP connection with extensions
        account_id: Account UUID
        folder_repo: Folder repository for DB operations
        auto_detect: Enable automatic special-use detection

    Returns:
        List of folder UUIDs that were upserted
    """
    server_folders = await extended.list_special_use()

    if not server_folders:
        logger.warning(
            "No folders discovered from server",
            extra={"account_id": str(account_id)},
        )
        return []

    existing = await folder_repo.get_by_account(account_id)
    existing_names = {f.imap_name for f in existing}
    server_names = {f.name for f in server_folders}

    folder_ids: list[uuid.UUID] = []

    for folder_info in server_folders:
        special_use = detect_special_use(folder_info) if auto_detect else folder_info.special_use

        db_folder = await folder_repo.upsert_folder(
            account_id=account_id,
            imap_name=folder_info.name,
            display_name=folder_info.name,
            special_use=special_use,
            separator=folder_info.separator,
            subscribed=True,
            flags=folder_info.flags,
        )
        folder_ids.append(db_folder.id)

    # Folders that disappeared from server
    missing = existing_names - server_names
    if missing:
        logger.info(
            "Folders no longer on server (keeping mail data)",
            extra={
                "account_id": str(account_id),
                "missing_folders": sorted(missing),
            },
        )

    new_folders = server_names - existing_names
    if new_folders:
        logger.info(
            "New folders discovered",
            extra={
                "account_id": str(account_id),
                "new_folders": sorted(new_folders),
            },
        )

    return folder_ids
