"""
Spam feedback handler.

When a user moves mail to/from spam, updates Qdrant tag and
logs a correction verdict in the database.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from mail_verdict.database.models import VerdictSource

if TYPE_CHECKING:
    from mail_verdict.database.repository import VerdictRepository
    from mail_verdict.semantic.store import SemanticStore

logger = logging.getLogger(__name__)


class SpamFeedbackHandler:
    """
    Processes user spam corrections.

    When a user moves mail into or out of the spam folder,
    updates the Qdrant is_spam tag and creates a user_feedback verdict.
    """

    def __init__(
        self,
        semantic_store: SemanticStore,
        verdict_repo: VerdictRepository,
    ) -> None:
        """
        Initialize the feedback handler.

        Args:
            semantic_store: Vector store for tag updates
            verdict_repo: Verdict persistence for correction logging
        """
        self._store = semantic_store
        self._verdict_repo = verdict_repo

    async def handle_moved_to_spam(
        self,
        mail_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> bool:
        """
        Handle a mail being moved to the spam folder by the user.

        Updates Qdrant tag to is_spam=true and logs a user_feedback verdict.

        Args:
            mail_id: Mail UUID
            account_id: Account UUID

        Returns:
            True if feedback was processed successfully
        """
        return await self._record_feedback(
            mail_id=mail_id,
            account_id=account_id,
            is_spam=True,
        )

    async def handle_moved_from_spam(
        self,
        mail_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> bool:
        """
        Handle a mail being moved out of the spam folder by the user.

        Updates Qdrant tag to is_spam=false and logs a user_feedback verdict.

        Args:
            mail_id: Mail UUID
            account_id: Account UUID

        Returns:
            True if feedback was processed successfully
        """
        return await self._record_feedback(
            mail_id=mail_id,
            account_id=account_id,
            is_spam=False,
        )

    async def _record_feedback(
        self,
        mail_id: uuid.UUID,
        account_id: uuid.UUID,
        is_spam: bool,
    ) -> bool:
        """
        Record user feedback: update Qdrant tag and create verdict.

        Args:
            mail_id: Mail UUID
            account_id: Account UUID
            is_spam: User's classification

        Returns:
            True if both Qdrant and Postgres updates succeeded
        """
        mail_id_str = str(mail_id)
        account_id_str = str(account_id)
        label = "spam" if is_spam else "not-spam"

        try:
            # Update Qdrant payload is_spam tag
            qdrant_ok = await self._update_qdrant_tag(mail_id_str, account_id_str, is_spam)

            if not qdrant_ok:
                logger.warning(
                    "Failed to update Qdrant tag for mail %s",
                    mail_id_str[:8],
                )

            # Log correction in Verdict table
            await self._verdict_repo.create_verdict(
                mail_id=mail_id,
                is_spam=is_spam,
                source=VerdictSource.USER_FEEDBACK,
            )

            logger.info(
                "User feedback recorded",
                extra={
                    "mail_id": mail_id_str[:8],
                    "feedback": label,
                },
            )
            return True

        except Exception:
            logger.exception(
                "Failed to record user feedback for mail %s",
                mail_id_str[:8],
            )
            return False

    async def _update_qdrant_tag(
        self,
        mail_id: str,
        account_id: str,
        is_spam: bool,
    ) -> bool:
        """
        Update the is_spam tag in Qdrant for a mail point.

        Args:
            mail_id: Mail UUID string (Qdrant point ID)
            account_id: Account UUID string
            is_spam: New spam classification

        Returns:
            True if update succeeded
        """
        return await self._store.update_payload(
            mail_id=mail_id,
            payload={"is_spam": str(is_spam).lower()},
        )
