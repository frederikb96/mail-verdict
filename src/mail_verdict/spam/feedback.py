"""
Spam feedback handler.

Two entry points, both writing a user_feedback verdict row:

- `handle_moved_to_spam` / `handle_moved_from_spam` -- unconditional,
  called from an explicit user action (POST /api/mails/{id}/feedback, an
  MCP tool call): the user directly said "this is/isn't spam", so it is
  recorded whatever the current verdict already says.
- `handle_folder_move_to_junk` / `handle_folder_move_out_of_junk` --
  called only from the folder-move listener (spam/processor.py), gated on
  whether the move contradicts the current verdict. The discriminator is
  deliberately not "who wrote the row": both the pipeline's own move-spam
  stage and a user dragging a message are the same application, and
  `origin` cannot tell them apart (it distinguishes PostIMAP from
  consumers, not the classifier from the user). Contradiction with the
  stored verdict is stateless and restart-safe, and it is something the
  classifier can never do to what it just wrote in the same run: by the
  time a move-spam effect's own move commits, its RecordVerdict effect
  already has, so the verdict this check reads back already agrees.
  Moving spam to trash is excluded on purpose: deleting a message already
  agreed to be spam is the ordinary outcome of a junk folder, not a
  correction.
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
    """Records user_feedback verdict rows from explicit actions and from
    folder moves that contradict the current verdict."""

    def __init__(self, verdict_repo: VerdictRepository) -> None:
        """
        Args:
            verdict_repo: Verdict persistence and the current-verdict read
        """
        self._verdict_repo = verdict_repo

    async def handle_moved_to_spam(self, mail_id: uuid.UUID, account_id: uuid.UUID) -> bool:
        """An explicit "this is spam" from the user. Unconditional.

        Args:
            mail_id: Mail UUID
            account_id: Account UUID

        Returns:
            True if feedback was recorded
        """
        return await self._record_feedback(mail_id, account_id, is_spam=True)

    async def handle_moved_from_spam(self, mail_id: uuid.UUID, account_id: uuid.UUID) -> bool:
        """An explicit "this is not spam" from the user. Unconditional.

        Args:
            mail_id: Mail UUID
            account_id: Account UUID

        Returns:
            True if feedback was recorded
        """
        return await self._record_feedback(mail_id, account_id, is_spam=False)

    async def handle_folder_move_to_junk(self, mail_id: uuid.UUID, account_id: uuid.UUID) -> bool:
        """
        A message landed in the junk folder. Records a correction only if
        the current verdict does not already say spam -- an ordinary move
        into junk with nothing to correct, and the pipeline's own
        move-spam effect having just written that same verdict, both look
        identical here and both correctly produce no feedback row.

        Args:
            mail_id: Mail UUID
            account_id: Account UUID

        Returns:
            True if a correction was recorded
        """
        current = await self._verdict_repo.get_current_verdict(mail_id)
        if current is not None and current.is_spam:
            return False
        return await self._record_feedback(mail_id, account_id, is_spam=True)

    async def handle_folder_move_out_of_junk(
        self, mail_id: uuid.UUID, account_id: uuid.UUID, *, destination_special_use: str | None,
    ) -> bool:
        """
        A message left the junk folder. Records a correction only if the
        current verdict says spam and the destination is not trash --
        deleting mail already agreed to be spam is the commonest thing
        anyone does in a junk folder, not a sign the classifier was wrong.

        Args:
            mail_id: Mail UUID
            account_id: Account UUID
            destination_special_use: The destination folder's effective
                special_use, or None

        Returns:
            True if a correction was recorded
        """
        if destination_special_use == "trash":
            return False
        current = await self._verdict_repo.get_current_verdict(mail_id)
        if current is not None and not current.is_spam:
            return False
        return await self._record_feedback(mail_id, account_id, is_spam=False)

    async def _record_feedback(
        self, mail_id: uuid.UUID, account_id: uuid.UUID, is_spam: bool,
    ) -> bool:
        """
        Record user feedback as a new verdict row.

        Args:
            mail_id: Mail UUID
            account_id: Account UUID
            is_spam: The corrected classification

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
