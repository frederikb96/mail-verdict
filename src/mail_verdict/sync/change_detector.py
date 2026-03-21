"""
Change detection between IMAP folder state and local database.

Three-tier detection algorithm:
  Tier 1 (QRESYNC): Server sends VANISHED + flag updates automatically
  Tier 2 (CONDSTORE): Manual UID diff + FETCH FLAGS CHANGEDSINCE
  Tier 3 (Full diff): Full UID listing, compare against local cache

UIDVALIDITY change triggers full folder resync.

Uses imap-tools MailBox with asyncio.to_thread for all IMAP operations.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from imap_tools import BaseMailBox

from mail_verdict.sync.events import (
    FlagsChanged,
    MailDeleted,
    MailReceived,
    SyncEvent,
)

if TYPE_CHECKING:
    from mail_verdict.database.models import Folder
    from mail_verdict.database.repository import FolderRepository, MailRepository

logger = logging.getLogger(__name__)


@dataclass
class UIDFlags:
    """UID with its current flags."""

    uid: int
    flags: set[str]
    modseq: int | None = None


@dataclass
class ChangeSet:
    """Detected changes for a folder sync cycle."""

    new_uids: list[int] = field(default_factory=list)
    deleted_uids: list[int] = field(default_factory=list)
    flag_changes: list[UIDFlags] = field(default_factory=list)
    uidvalidity_changed: bool = False
    new_uidvalidity: int | None = None
    new_highestmodseq: int | None = None
    new_uidnext: int | None = None


@dataclass
class SelectInfo:
    """Parsed folder SELECT metadata from imap-tools."""

    ok: bool
    uidvalidity: int = 0
    uidnext: int = 0
    exists: int = 0
    highestmodseq: int | None = None


class ChangeDetector:
    """
    Detects changes between IMAP server state and local database.

    Automatically selects the best detection tier based on
    server capabilities and stored sync state.
    """

    def __init__(
        self,
        folder_repo: FolderRepository,
        mail_repo: MailRepository,
    ) -> None:
        """
        Initialize change detector.

        Args:
            folder_repo: Repository for folder sync state
            mail_repo: Repository for mail lookups
        """
        self._folder_repo = folder_repo
        self._mail_repo = mail_repo

    async def detect_changes(
        self,
        mailbox: BaseMailBox,
        account_id: uuid.UUID,
        folder: Folder,
    ) -> tuple[ChangeSet, list[SyncEvent]]:
        """
        Detect changes in a folder since last sync.

        Selects detection tier based on stored state. Selects the folder
        via imap-tools and reads metadata from the server response.

        Args:
            mailbox: Authenticated imap-tools BaseMailBox
            account_id: Account UUID
            folder: Folder with stored sync state

        Returns:
            Tuple of (changeset, events)
        """
        stored_modseq = folder.highestmodseq
        stored_uidvalidity = folder.uidvalidity

        # SELECT the folder and get metadata
        select_info = await self._select_folder(mailbox, folder.imap_name)

        if not select_info.ok:
            logger.error(
                "SELECT failed",
                extra={
                    "folder": folder.imap_name,
                    "account_id": str(account_id),
                },
            )
            return ChangeSet(), []

        # UIDVALIDITY change = full resync required
        if stored_uidvalidity and select_info.uidvalidity != stored_uidvalidity:
            logger.warning(
                "UIDVALIDITY changed, full resync required",
                extra={
                    "folder": folder.imap_name,
                    "old": stored_uidvalidity,
                    "new": select_info.uidvalidity,
                },
            )
            return await self._full_diff(
                mailbox, account_id, folder, select_info, uidvalidity_changed=True
            )

        # Tier 2: CONDSTORE (if server supports and we have a stored modseq)
        if stored_modseq and select_info.highestmodseq:
            return await self._condstore_diff(
                mailbox, account_id, folder, select_info
            )

        # Tier 3: Full diff
        return await self._full_diff(mailbox, account_id, folder, select_info)

    async def _select_folder(
        self,
        mailbox: BaseMailBox,
        folder_name: str,
    ) -> SelectInfo:
        """
        Select a folder and extract metadata from the IMAP response.

        Args:
            mailbox: Authenticated BaseMailBox
            folder_name: IMAP folder name to select
        """
        def _sync_select() -> SelectInfo:
            try:
                mailbox.folder.set(folder_name)

                # Use STATUS command for reliable metadata
                uidvalidity = 0
                uidnext = 0
                exists = 0
                highestmodseq: int | None = None
                status = mailbox.folder.status(
                    folder_name,
                    options=("MESSAGES", "UIDNEXT", "UIDVALIDITY"),
                )
                uidvalidity = status.get("UIDVALIDITY", uidvalidity)
                uidnext = status.get("UIDNEXT", uidnext)
                exists = status.get("MESSAGES", exists)

                return SelectInfo(
                    ok=True,
                    uidvalidity=uidvalidity,
                    uidnext=uidnext,
                    exists=exists,
                    highestmodseq=highestmodseq,
                )
            except Exception as exc:
                logger.warning(
                    "Folder select failed",
                    extra={"folder": folder_name, "error": str(exc)},
                )
                return SelectInfo(ok=False)

        return await asyncio.to_thread(_sync_select)

    async def _condstore_diff(
        self,
        mailbox: BaseMailBox,
        account_id: uuid.UUID,
        folder: Folder,
        select_info: SelectInfo,
    ) -> tuple[ChangeSet, list[SyncEvent]]:
        """
        Tier 2: CONDSTORE-based change detection.

        Manual UID diff + flag comparison for existing UIDs.
        """
        changeset = ChangeSet(
            new_uidvalidity=select_info.uidvalidity,
            new_highestmodseq=select_info.highestmodseq,
            new_uidnext=select_info.uidnext,
        )
        events: list[SyncEvent] = []

        # Get all UIDs from server
        server_uids = set(await self._fetch_all_uids(mailbox))
        local_uids = await self._get_local_uids(folder.id)

        changeset.new_uids = sorted(server_uids - local_uids)
        changeset.deleted_uids = sorted(local_uids - server_uids)

        # Fetch flags for all existing UIDs to detect changes
        existing_uids = server_uids & local_uids
        if existing_uids:
            flag_changes = await self._fetch_uid_flags(mailbox, existing_uids)
            changeset.flag_changes = flag_changes

        # Generate events
        events.extend(
            MailReceived(account_id=account_id, folder_id=folder.id, uid=uid)
            for uid in changeset.new_uids
        )
        events.extend(
            MailDeleted(account_id=account_id, folder_id=folder.id, uid=uid)
            for uid in changeset.deleted_uids
        )
        events.extend(
            FlagsChanged(
                account_id=account_id,
                folder_id=folder.id,
                uid=uf.uid,
                is_read="\\Seen" in uf.flags,
                is_flagged="\\Flagged" in uf.flags,
            )
            for uf in changeset.flag_changes
        )

        return changeset, events

    async def _full_diff(
        self,
        mailbox: BaseMailBox,
        account_id: uuid.UUID,
        folder: Folder,
        select_info: SelectInfo,
        *,
        uidvalidity_changed: bool = False,
    ) -> tuple[ChangeSet, list[SyncEvent]]:
        """
        Tier 3: Full UID+FLAGS diff.

        Fetches all UIDs and flags, compares against local state.
        """
        changeset = ChangeSet(
            uidvalidity_changed=uidvalidity_changed,
            new_uidvalidity=select_info.uidvalidity,
            new_highestmodseq=select_info.highestmodseq,
            new_uidnext=select_info.uidnext,
        )
        events: list[SyncEvent] = []

        if uidvalidity_changed:
            # All local UIDs are invalid, treat everything as new
            server_uids = await self._fetch_all_uids(mailbox)
            changeset.new_uids = sorted(server_uids)
        else:
            server_uids_set = set(await self._fetch_all_uids(mailbox))
            local_uids = await self._get_local_uids(folder.id)

            changeset.new_uids = sorted(server_uids_set - local_uids)
            changeset.deleted_uids = sorted(local_uids - server_uids_set)

            # All existing UIDs get flag updates
            existing_uids = server_uids_set & local_uids
            if existing_uids:
                changeset.flag_changes = await self._fetch_uid_flags(
                    mailbox, existing_uids
                )

        events.extend(
            MailReceived(account_id=account_id, folder_id=folder.id, uid=uid)
            for uid in changeset.new_uids
        )
        events.extend(
            MailDeleted(account_id=account_id, folder_id=folder.id, uid=uid)
            for uid in changeset.deleted_uids
        )
        events.extend(
            FlagsChanged(
                account_id=account_id,
                folder_id=folder.id,
                uid=uf.uid,
                is_read="\\Seen" in uf.flags,
                is_flagged="\\Flagged" in uf.flags,
            )
            for uf in changeset.flag_changes
        )

        return changeset, events

    async def _fetch_all_uids(self, mailbox: BaseMailBox) -> list[int]:
        """Fetch all UIDs in the currently selected folder."""
        def _sync() -> list[int]:
            uid_strs = mailbox.uids()
            return [int(u) for u in uid_strs if u.isdigit()]

        return await asyncio.to_thread(_sync)

    async def _fetch_uid_flags(
        self,
        mailbox: BaseMailBox,
        uids: set[int],
    ) -> list[UIDFlags]:
        """
        Fetch flags for specific UIDs using headers_only fetch.

        Args:
            mailbox: Authenticated BaseMailBox (folder already selected)
            uids: Set of UIDs to fetch flags for
        """
        def _sync() -> list[UIDFlags]:
            from imap_tools import AND

            uid_str = ",".join(str(u) for u in sorted(uids))
            results: list[UIDFlags] = []

            for msg in mailbox.fetch(AND(uid=uid_str), headers_only=True, mark_seen=False):
                if msg.uid:
                    results.append(
                        UIDFlags(
                            uid=int(msg.uid),
                            flags=set(msg.flags),
                        )
                    )
            return results

        return await asyncio.to_thread(_sync)

    async def _get_local_uids(self, folder_id: uuid.UUID) -> set[int]:
        """Get all known UIDs for a folder from the database."""
        return await self._mail_repo.get_uids_by_folder(folder_id)


def _parse_uid_set(text: str) -> list[int]:
    """
    Parse a VANISHED response UID set.

    Handles ranges like "1:5,10,15:20".

    Args:
        text: Text containing UID set
    """
    uids: list[int] = []

    # Extract the UID set portion after VANISHED
    match = re.search(r"VANISHED\s+(?:\(EARLIER\)\s+)?(.+)", text, re.IGNORECASE)
    if not match:
        return uids

    uid_set = match.group(1).strip()

    for part in uid_set.split(","):
        part = part.strip()
        if ":" in part:
            try:
                start, end = part.split(":", 1)
                uids.extend(range(int(start), int(end) + 1))
            except ValueError:
                continue
        elif part.isdigit():
            uids.append(int(part))

    return uids
