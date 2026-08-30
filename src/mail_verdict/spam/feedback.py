"""
Spam feedback handler.

When a user moves mail to/from the junk folder, or submits feedback via the
API, logs a correction verdict in the database.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from mail_verdict.database.models import VerdictSource

if TYPE_CHECKING:
    from mail_verdict.database.repository import VerdictRepository

logger = logging.getLogger(__name__)


class SpamFeedbackHandler:
    """Processes user spam corrections into user_feedback verdict rows."""

    def __init__(self, verdict_repo: VerdictRepository) -> None:
        """
        Initialize the feedback handler.

        Args:
            verdict_repo: Verdict persistence for correction logging
        """
        self._verdict_repo = verdict_repo

    async def handle_moved_to_spam(self, mail_id: uuid.UUID, account_id: uuid.UUID) -> bool:
        """
        Handle a mail being moved to the junk folder by the user.

        Args:
            mail_id: Mail UUID
            account_id: Account UUID

        Returns:
            True if feedback was recorded
        """
        return await self._record_feedback(mail_id, account_id, is_spam=True)

    async def handle_moved_from_spam(self, mail_id: uuid.UUID, account_id: uuid.UUID) -> bool:
        """
        Handle a mail being moved out of the junk folder by the user.

        Args:
            mail_id: Mail UUID
            account_id: Account UUID

        Returns:
            True if feedback was recorded
        """
        return await self._record_feedback(mail_id, account_id, is_spam=False)

    async def _record_feedback(
        self, mail_id: uuid.UUID, account_id: uuid.UUID, is_spam: bool,
    ) -> bool:
        """
        Record user feedback as a new verdict row.

        Args:
            mail_id: Mail UUID
            account_id: Account UUID
            is_spam: User's classification

        Returns:
            True if the verdict was recorded successfully
        """
        try:
            await self._verdict_repo.create_verdict(
                mail_id=mail_id,
                account_id=account_id,
                is_spam=is_spam,
                source=VerdictSource.USER_FEEDBACK,
            )
            logger.info(
                "User feedback recorded",
                extra={"mail_id": str(mail_id)[:8], "is_spam": is_spam},
            )
            return True
        except Exception:
            logger.exception("Failed to record user feedback for mail %s", str(mail_id)[:8])
            return False
