"""
Tag storage with IMAP keyword sync.

Postgres mail_tags table (already exists) + best-effort IMAP keyword sync.
Checks PERMANENTFLAGS before writing custom keywords to IMAP.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from mail_verdict.database.models import TagSource
from mail_verdict.sync.actions import ActionPropagator, ActionType, IMAPAction

if TYPE_CHECKING:
    from mail_verdict.database.repository import TagRepository

logger = logging.getLogger(__name__)


class TagSyncService:
    """
    Manages tag persistence in Postgres with best-effort IMAP keyword sync.

    On tag add:
        1. Write to Postgres via TagRepository
        2. Try IMAP STORE +FLAGS keyword (if server supports custom keywords)

    On tag remove:
        1. Remove from Postgres
        2. Try IMAP STORE -FLAGS keyword
    """

    def __init__(
        self,
        tag_repo: TagRepository,
        propagator: ActionPropagator | None = None,
    ) -> None:
        """
        Initialize tag sync service.

        Args:
            tag_repo: Repository for Postgres tag operations
            propagator: IMAP action propagator for keyword sync
        """
        self._tag_repo = tag_repo
        self._propagator = propagator

    async def add_tag(
        self,
        mail_id: uuid.UUID,
        tag_name: str,
        source: TagSource,
        *,
        folder: str = "",
        uid: int = 0,
        sync_imap: bool = True,
    ) -> None:
        """
        Add a tag to a mail with optional IMAP sync.

        Args:
            mail_id: Database mail UUID
            tag_name: Tag string to add
            source: Where this tag came from (rule, enrichment, user, etc.)
            folder: IMAP folder for keyword sync
            uid: IMAP UID for keyword sync
            sync_imap: Whether to attempt IMAP keyword sync
        """
        await self._tag_repo.add_tag(mail_id, tag_name, source)

        if sync_imap and self._propagator and folder and uid:
            try:
                await self._propagator.execute_imap(
                    IMAPAction(
                        action_type=ActionType.STORE_FLAGS,
                        folder=folder,
                        uid_set=str(uid),
                        flags_add=[tag_name],
                    )
                )
            except Exception:
                logger.debug(
                    "IMAP keyword sync failed (Postgres tag still saved)",
                    extra={"tag": tag_name, "folder": folder},
                )

    async def remove_tag(
        self,
        mail_id: uuid.UUID,
        tag_name: str,
        *,
        folder: str = "",
        uid: int = 0,
        sync_imap: bool = True,
    ) -> bool:
        """
        Remove a tag from a mail with optional IMAP sync.

        Args:
            mail_id: Database mail UUID
            tag_name: Tag string to remove
            folder: IMAP folder for keyword sync
            uid: IMAP UID for keyword sync
            sync_imap: Whether to attempt IMAP keyword sync

        Returns:
            True if tag was removed from Postgres
        """
        removed = await self._tag_repo.remove_tag(mail_id, tag_name)

        if sync_imap and self._propagator and folder and uid:
            try:
                await self._propagator.execute_imap(
                    IMAPAction(
                        action_type=ActionType.STORE_FLAGS,
                        folder=folder,
                        uid_set=str(uid),
                        flags_remove=[tag_name],
                    )
                )
            except Exception:
                logger.debug(
                    "IMAP keyword removal failed",
                    extra={"tag": tag_name, "folder": folder},
                )

        return removed

    async def import_imap_keywords(
        self,
        mail_id: uuid.UUID,
        keywords: list[str],
    ) -> int:
        """
        Import existing IMAP keywords as tags (source=imap).

        Called during sync to capture server-side keywords into Postgres.

        Args:
            mail_id: Database mail UUID
            keywords: IMAP keyword strings to import

        Returns:
            Number of tags imported
        """
        count = 0
        for keyword in keywords:
            clean = keyword.strip()
            if not clean or clean.startswith("\\"):
                continue
            try:
                await self._tag_repo.add_tag(mail_id, clean, TagSource.IMAP)
                count += 1
            except Exception:
                logger.debug(
                    "Failed to import IMAP keyword as tag",
                    extra={"keyword": clean},
                )
        return count
