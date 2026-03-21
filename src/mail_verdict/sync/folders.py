"""
Folder discovery and type detection.

Discovers IMAP folders via imap-tools, auto-detects types via RFC 6154
SPECIAL-USE flags with case-insensitive name fallback, handles dedup
when multiple folders map to the same special_use, and syncs against
stored folder state.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from imap_tools import BaseMailBox
from imap_tools import FolderInfo as ImapToolsFolderInfo

if TYPE_CHECKING:
    from mail_verdict.database.repository import FolderRepository

logger = logging.getLogger(__name__)


# RFC 6154 special-use flag to SpecialUse enum value mapping
SPECIAL_USE_FLAGS: dict[str, str] = {
    "\\All": "all",
    "\\Archive": "archive",
    "\\Drafts": "drafts",
    "\\Flagged": "flagged",
    "\\Junk": "junk",
    "\\Sent": "sent",
    "\\Trash": "trash",
}


# Case-insensitive folder name patterns for special-use detection fallback.
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
    "junk mail": "junk",
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


@dataclass
class FolderInfo:
    """Parsed folder info from imap-tools LIST response."""

    name: str
    separator: str
    flags: list[str] = field(default_factory=list)
    special_use: str | None = None


def _parse_imap_tools_folder(fi: ImapToolsFolderInfo) -> FolderInfo:
    """
    Convert imap-tools FolderInfo to our internal FolderInfo.

    Extracts special-use flags from the folder flags list.

    Args:
        fi: imap-tools FolderInfo object
    """
    flags = list(fi.flags) if fi.flags else []
    special_use: str | None = None

    for flag in flags:
        if flag in SPECIAL_USE_FLAGS:
            special_use = SPECIAL_USE_FLAGS[flag]
            break

    return FolderInfo(
        name=fi.name,
        separator=fi.delim,
        flags=flags,
        special_use=special_use,
    )


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
    mailbox: BaseMailBox,
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
        mailbox: Authenticated imap-tools BaseMailBox
        account_id: Account UUID
        folder_repo: Folder repository for DB operations
        auto_detect: Enable automatic special-use detection

    Returns:
        List of folder UUIDs that were upserted
    """
    def _sync_list() -> list[FolderInfo]:
        raw_folders = mailbox.folder.list()
        return [_parse_imap_tools_folder(f) for f in raw_folders]

    server_folders = await asyncio.to_thread(_sync_list)

    if not server_folders:
        logger.warning(
            "No folders discovered from server",
            extra={"account_id": str(account_id)},
        )
        return []

    # Dedup: if multiple folders map to same special_use, RFC 6154 flag wins
    seen_special_use: dict[str, FolderInfo] = {}
    for folder_info in server_folders:
        su = detect_special_use(folder_info) if auto_detect else folder_info.special_use
        if su and su in seen_special_use:
            existing = seen_special_use[su]
            # RFC 6154 flag wins over name-based detection
            if folder_info.special_use and not existing.special_use:
                seen_special_use[su] = folder_info
        elif su:
            seen_special_use[su] = folder_info

    existing_folders = await folder_repo.get_by_account(account_id)
    existing_names = {f.imap_name for f in existing_folders}
    server_names = {f.name for f in server_folders}

    folder_ids: list[uuid.UUID] = []

    for folder_info in server_folders:
        raw_special_use = (
            detect_special_use(folder_info) if auto_detect else folder_info.special_use
        )
        # Only assign special_use if this folder is the primary for that type
        special_use = raw_special_use
        if raw_special_use and seen_special_use.get(raw_special_use) is not folder_info:
            special_use = None

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
