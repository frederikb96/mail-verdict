"""
Tag storage — Postgres only.

Manages tags in the mail_tags table. IMAP keyword sync is handled by
PostIMAP PG triggers when the message keywords column is updated.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from mail_verdict.database.models import TagSource

if TYPE_CHECKING:
    from mail_verdict.database.repository import TagRepository

logger = logging.getLogger(__name__)


class TagSyncService:
    """
    Manages tag persistence in Postgres.

    On tag add: write to Postgres via TagRepository.
    On tag remove: remove from Postgres via TagRepository.

    IMAP keyword sync is no longer managed here -- PostIMAP handles
    keyword propagation via PG triggers on the messages table.
    """

    def __init__(
        self,
        tag_repo: TagRepository,
    ) -> None:
        """
        Initialize tag sync service.

        Args:
            tag_repo: Repository for Postgres tag operations
        """
        self._tag_repo = tag_repo

    async def add_tag(
        self,
        mail_id: uuid.UUID,
        tag_name: str,
        source: TagSource,
    ) -> None:
        """
        Add a tag to a message in Postgres.

        Args:
            mail_id: Database message UUID
            tag_name: Tag string to add
            source: Where this tag came from (rule, enrichment, user, etc.)
        """
        await self._tag_repo.add_tag(mail_id, tag_name, source)

    async def remove_tag(
        self,
        mail_id: uuid.UUID,
        tag_name: str,
    ) -> bool:
        """
        Remove a tag from a message in Postgres.

        Args:
            mail_id: Database message UUID
            tag_name: Tag to remove

        Returns:
            True if tag was removed from Postgres
        """
        return await self._tag_repo.remove_tag(mail_id, tag_name)

    async def import_imap_keywords(
        self,
        mail_id: uuid.UUID,
        keywords: list[str],
    ) -> int:
        """
        Import existing IMAP keywords as tags (source=imap).

        Called during sync to capture server-side keywords into Postgres.

        Args:
            mail_id: Database message UUID
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
