"""
Change detection between IMAP folder state and local database.

Three-tier detection algorithm:
  Tier 1 (QRESYNC): Server sends VANISHED + flag updates automatically
  Tier 2 (CONDSTORE): Manual UID diff + FETCH FLAGS CHANGEDSINCE
  Tier 3 (Full diff): UID FETCH 1:* (FLAGS), compare against local cache

UIDVALIDITY change triggers full folder resync.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mail_verdict.sync.events import (
    FlagsChanged,
    MailDeleted,
    MailReceived,
    SyncEvent,
)
from mail_verdict.sync.extensions import AsyncIMAPExtended, SelectResult

if TYPE_CHECKING:
    from mail_verdict.database.models import Folder
    from mail_verdict.database.repository import FolderRepository, MailRepository

logger = logging.getLogger(__name__)

# Pattern for parsing UID FETCH flags response
# e.g., b'1 FETCH (UID 42 FLAGS (\\Seen \\Flagged) MODSEQ (12345))'
_FETCH_FLAGS_RE = re.compile(
    rb"(\d+)\s+FETCH\s+\(.*?UID\s+(\d+).*?FLAGS\s+\(([^)]*)\)"
    rb"(?:.*?MODSEQ\s+\((\d+)\))?",
)


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
        extended: AsyncIMAPExtended,
        account_id: uuid.UUID,
        folder: Folder,
    ) -> tuple[ChangeSet, list[SyncEvent]]:
        """
        Detect changes in a folder since last sync.

        Selects detection tier based on capabilities and stored state.
        UIDVALIDITY change forces full resync.

        Args:
            extended: Active IMAP connection
            account_id: Account UUID
            folder: Folder with stored sync state

        Returns:
            Tuple of (changeset, events)
        """
        has_condstore = extended.has_capability("CONDSTORE")
        has_qresync = extended.has_capability("QRESYNC")
        stored_modseq = folder.highestmodseq
        stored_uidvalidity = folder.uidvalidity

        # SELECT the folder with appropriate extensions.
        # QRESYNC requires ENABLE QRESYNC before use; fall back if rejected.
        select_result: SelectResult | None = None

        if has_qresync and stored_uidvalidity and stored_modseq:
            select_result = await extended.select_qresync(
                folder.imap_name,
                stored_uidvalidity,
                stored_modseq,
            )
            if not select_result.ok:
                logger.info(
                    "QRESYNC SELECT rejected, falling back to CONDSTORE",
                    extra={"folder": folder.imap_name},
                )
                select_result = None
                has_qresync = False

        if select_result is None and has_condstore:
            select_result = await extended.select_condstore(folder.imap_name)

        if select_result is None:
            select_result = await extended.select_plain(folder.imap_name)

        if not select_result.ok:
            logger.error(
                "SELECT failed",
                extra={
                    "folder": folder.imap_name,
                    "account_id": str(account_id),
                    "raw_lines": [
                        line.decode(errors="replace") if isinstance(line, bytes) else str(line)
                        for line in select_result.raw_lines
                    ],
                },
            )
            return ChangeSet(), []

        # UIDVALIDITY change = full resync required
        if stored_uidvalidity and select_result.uidvalidity != stored_uidvalidity:
            logger.warning(
                "UIDVALIDITY changed, full resync required",
                extra={
                    "folder": folder.imap_name,
                    "old": stored_uidvalidity,
                    "new": select_result.uidvalidity,
                },
            )
            return await self._full_diff(
                extended, account_id, folder, select_result, uidvalidity_changed=True
            )

        # Tier 1: QRESYNC (only if QRESYNC SELECT succeeded above)
        if has_qresync and stored_modseq:
            return await self._qresync_diff(extended, account_id, folder, select_result)

        # Tier 2: CONDSTORE
        if has_condstore and stored_modseq:
            return await self._condstore_diff(extended, account_id, folder, select_result)

        # Tier 3: Full diff
        return await self._full_diff(extended, account_id, folder, select_result)

    async def _qresync_diff(
        self,
        extended: AsyncIMAPExtended,
        account_id: uuid.UUID,
        folder: Folder,
        select_result: SelectResult,
    ) -> tuple[ChangeSet, list[SyncEvent]]:
        """
        Tier 1: QRESYNC-based change detection.

        The server already sent VANISHED and flag updates in the SELECT response.
        We also check for new UIDs beyond stored uidnext.
        """
        changeset = ChangeSet(
            new_uidvalidity=select_result.uidvalidity,
            new_highestmodseq=select_result.highestmodseq,
            new_uidnext=select_result.uidnext,
        )
        events: list[SyncEvent] = []

        # Parse VANISHED responses from SELECT
        for line in select_result.raw_lines:
            text = line.decode(errors="replace") if isinstance(line, bytes) else str(line)
            if "VANISHED" in text.upper():
                vanished_uids = _parse_uid_set(text)
                changeset.deleted_uids.extend(vanished_uids)

        # Check for new UIDs
        stored_uidnext = folder.uidnext or 1
        if select_result.uidnext and select_result.uidnext > stored_uidnext:
            new_uids = await self._fetch_uid_range(
                extended, stored_uidnext, select_result.uidnext - 1
            )
            changeset.new_uids = new_uids

        # Fetch flag changes since stored modseq
        if folder.highestmodseq and select_result.highestmodseq:
            if select_result.highestmodseq > folder.highestmodseq:
                flag_changes = await self._fetch_changed_flags(extended, folder.highestmodseq)
                changeset.flag_changes = flag_changes

        # Generate events
        for uid in changeset.new_uids:
            events.append(
                MailReceived(
                    account_id=account_id,
                    folder_id=folder.id,
                    uid=uid,
                )
            )

        for uid in changeset.deleted_uids:
            events.append(
                MailDeleted(
                    account_id=account_id,
                    folder_id=folder.id,
                    uid=uid,
                )
            )

        for uf in changeset.flag_changes:
            events.append(
                FlagsChanged(
                    account_id=account_id,
                    folder_id=folder.id,
                    uid=uf.uid,
                    is_read="\\Seen" in uf.flags,
                    is_flagged="\\Flagged" in uf.flags,
                )
            )

        return changeset, events

    async def _condstore_diff(
        self,
        extended: AsyncIMAPExtended,
        account_id: uuid.UUID,
        folder: Folder,
        select_result: SelectResult,
    ) -> tuple[ChangeSet, list[SyncEvent]]:
        """
        Tier 2: CONDSTORE-based change detection.

        Manual UID diff + FETCH FLAGS CHANGEDSINCE.
        """
        changeset = ChangeSet(
            new_uidvalidity=select_result.uidvalidity,
            new_highestmodseq=select_result.highestmodseq,
            new_uidnext=select_result.uidnext,
        )
        events: list[SyncEvent] = []

        # Get all UIDs from server
        server_uids = set(await self._fetch_all_uids(extended))
        local_uids = await self._get_local_uids(folder.id)

        changeset.new_uids = sorted(server_uids - local_uids)
        changeset.deleted_uids = sorted(local_uids - server_uids)

        # Fetch flag changes since stored modseq
        if folder.highestmodseq:
            changeset.flag_changes = await self._fetch_changed_flags(extended, folder.highestmodseq)

        # Generate events
        for uid in changeset.new_uids:
            events.append(
                MailReceived(
                    account_id=account_id,
                    folder_id=folder.id,
                    uid=uid,
                )
            )
        for uid in changeset.deleted_uids:
            events.append(
                MailDeleted(
                    account_id=account_id,
                    folder_id=folder.id,
                    uid=uid,
                )
            )
        for uf in changeset.flag_changes:
            events.append(
                FlagsChanged(
                    account_id=account_id,
                    folder_id=folder.id,
                    uid=uf.uid,
                    is_read="\\Seen" in uf.flags,
                    is_flagged="\\Flagged" in uf.flags,
                )
            )

        return changeset, events

    async def _full_diff(
        self,
        extended: AsyncIMAPExtended,
        account_id: uuid.UUID,
        folder: Folder,
        select_result: SelectResult,
        *,
        uidvalidity_changed: bool = False,
    ) -> tuple[ChangeSet, list[SyncEvent]]:
        """
        Tier 3: Full UID+FLAGS diff.

        Fetches all UIDs and flags, compares against local state.
        """
        changeset = ChangeSet(
            uidvalidity_changed=uidvalidity_changed,
            new_uidvalidity=select_result.uidvalidity,
            new_highestmodseq=select_result.highestmodseq,
            new_uidnext=select_result.uidnext,
        )
        events: list[SyncEvent] = []

        if uidvalidity_changed:
            # All local UIDs are invalid, treat everything as new
            server_flags = await self._fetch_all_uid_flags(extended)
            changeset.new_uids = [uf.uid for uf in server_flags]
        else:
            server_flags = await self._fetch_all_uid_flags(extended)
            server_uids = {uf.uid for uf in server_flags}
            local_uids = await self._get_local_uids(folder.id)

            changeset.new_uids = sorted(server_uids - local_uids)
            changeset.deleted_uids = sorted(local_uids - server_uids)

            # All existing UIDs get flag updates
            for uf in server_flags:
                if uf.uid in local_uids:
                    changeset.flag_changes.append(uf)

        for uid in changeset.new_uids:
            events.append(
                MailReceived(
                    account_id=account_id,
                    folder_id=folder.id,
                    uid=uid,
                )
            )
        for uid in changeset.deleted_uids:
            events.append(
                MailDeleted(
                    account_id=account_id,
                    folder_id=folder.id,
                    uid=uid,
                )
            )
        for uf in changeset.flag_changes:
            events.append(
                FlagsChanged(
                    account_id=account_id,
                    folder_id=folder.id,
                    uid=uf.uid,
                    is_read="\\Seen" in uf.flags,
                    is_flagged="\\Flagged" in uf.flags,
                )
            )

        return changeset, events

    async def _fetch_all_uids(self, extended: AsyncIMAPExtended) -> list[int]:
        """Fetch all UIDs in the currently selected folder via UID FETCH."""
        response = await extended.client.uid("FETCH", "1:*", "(FLAGS)")
        if response.result != "OK":
            return []

        uid_flags = _parse_fetch_flags(response.lines)
        return [uf.uid for uf in uid_flags]

    async def _fetch_uid_range(
        self,
        extended: AsyncIMAPExtended,
        start: int,
        end: int,
    ) -> list[int]:
        """Fetch UIDs in a given range via UID FETCH."""
        response = await extended.client.uid("FETCH", f"{start}:{end}", "(FLAGS)")
        if response.result != "OK":
            return []

        uid_flags = _parse_fetch_flags(response.lines)
        return [uf.uid for uf in uid_flags]

    async def _fetch_all_uid_flags(
        self,
        extended: AsyncIMAPExtended,
    ) -> list[UIDFlags]:
        """Fetch all UIDs with their FLAGS."""
        response = await extended.client.uid("FETCH", "1:*", "(FLAGS)")
        if response.result != "OK":
            return []

        return _parse_fetch_flags(response.lines)

    async def _fetch_changed_flags(
        self,
        extended: AsyncIMAPExtended,
        since_modseq: int,
    ) -> list[UIDFlags]:
        """Fetch flags changed since a given MODSEQ."""
        response = await extended.client.uid(
            "FETCH", "1:*", f"(FLAGS) (CHANGEDSINCE {since_modseq})"
        )
        if response.result != "OK":
            return []

        return _parse_fetch_flags(response.lines)

    async def _get_local_uids(self, folder_id: uuid.UUID) -> set[int]:
        """Get all known UIDs for a folder from the database."""
        return await self._mail_repo.get_uids_by_folder(folder_id)


def _parse_fetch_flags(lines: list[bytes]) -> list[UIDFlags]:
    """
    Parse FETCH response lines into UIDFlags.

    Args:
        lines: Raw response lines
    """
    results: list[UIDFlags] = []

    for line in lines:
        if not isinstance(line, bytes):
            continue

        match = _FETCH_FLAGS_RE.search(line)
        if not match:
            continue

        uid = int(match.group(2))
        flags_raw = match.group(3).decode(errors="replace")
        flags = {f.strip() for f in flags_raw.split() if f.strip()}
        modseq = int(match.group(4)) if match.group(4) else None

        results.append(UIDFlags(uid=uid, flags=flags, modseq=modseq))

    return results


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
