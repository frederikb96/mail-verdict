"""
Spam event processor.

Handles PG LISTEN events for new messages, routes them to
VerdictPipeline (for new messages) and SpamFeedbackHandler
(for user folder moves).
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.database.repository import FolderRepository, MessageRepository
    from mail_verdict.spam.feedback import SpamFeedbackHandler
    from mail_verdict.spam.pipeline import VerdictPipeline

logger = logging.getLogger(__name__)


class SpamEventProcessor:
    """
    Processes PG LISTEN events for spam analysis.

    Routes events to the appropriate handler:
    - New message INSERT -> VerdictPipeline.process_message()
    - Message folder change (moved to junk) -> SpamFeedbackHandler.handle_moved_to_spam()
    - Message folder change (moved from junk) -> SpamFeedbackHandler.handle_moved_from_spam()
    """

    def __init__(
        self,
        pipeline: VerdictPipeline,
        feedback: SpamFeedbackHandler,
        message_repo: MessageRepository,
        folder_repo: FolderRepository,
        db: DatabaseConnection,
    ) -> None:
        """
        Initialize the spam event processor.

        Args:
            pipeline: VerdictPipeline for new message analysis
            feedback: SpamFeedbackHandler for user corrections
            message_repo: Message repository for lookups
            folder_repo: Folder repository for lookups
            db: Database connection for queries
        """
        self._pipeline = pipeline
        self._feedback = feedback
        self._message_repo = message_repo
        self._folder_repo = folder_repo
        self._db = db

    async def handle_message_event(self, event: dict[str, Any]) -> None:
        """
        Handle a message event from PG LISTEN.

        Dispatches to the appropriate handler based on the operation type.

        Args:
            event: PG LISTEN event payload with keys:
                - op: "insert" or "update"
                - id: Message UUID string
                - account_id: Account UUID string
                - folder_id: Current folder UUID string
                - old_folder_id: Previous folder UUID (for updates/moves)
        """
        op = event.get("op")

        if op == "insert":
            await self._handle_new_message(event)
        elif op == "update":
            await self._handle_message_update(event)

    async def _handle_new_message(self, event: dict[str, Any]) -> None:
        """
        Handle new message: look up full message + folder, run verdict pipeline.

        Args:
            event: PG LISTEN event payload
        """
        try:
            message_id = uuid.UUID(event["id"])
            account_id = uuid.UUID(event["account_id"])
            folder_id = uuid.UUID(event["folder_id"])
        except (KeyError, ValueError) as exc:
            logger.warning("Invalid message event payload: %s", exc)
            return

        msg = await self._message_repo.get_by_id(account_id, message_id)
        if msg is None:
            logger.debug("Message %s not found for spam check", str(message_id)[:8])
            return

        folder = await self._folder_repo.get_by_id(folder_id)
        if folder is None:
            logger.debug("Folder %s not found for spam check", str(folder_id)[:8])
            return

        await self._pipeline.process_message(msg, folder)

    async def _handle_message_update(self, event: dict[str, Any]) -> None:
        """
        Handle message update: check if folder changed (user move to/from spam).

        Args:
            event: PG LISTEN event payload
        """
        old_folder_id_str = event.get("old_folder_id")
        new_folder_id_str = event.get("folder_id")

        if not old_folder_id_str or not new_folder_id_str:
            return

        if old_folder_id_str == new_folder_id_str:
            return  # Not a folder move

        try:
            message_id = uuid.UUID(event["id"])
            account_id = uuid.UUID(event["account_id"])
            old_folder_id = uuid.UUID(old_folder_id_str)
            new_folder_id = uuid.UUID(new_folder_id_str)
        except (KeyError, ValueError) as exc:
            logger.warning("Invalid message update event payload: %s", exc)
            return

        # Look up folder special_use for both old and new folders
        old_folder = await self._folder_repo.get_by_id(old_folder_id)
        new_folder = await self._folder_repo.get_by_id(new_folder_id)

        if old_folder and old_folder.special_use == "junk":
            # Moved FROM spam -> user correction (not-spam)
            await self._feedback.handle_moved_from_spam(
                mail_id=message_id,
                account_id=account_id,
            )
        elif new_folder and new_folder.special_use == "junk":
            # Moved TO spam -> user feedback (spam)
            await self._feedback.handle_moved_to_spam(
                mail_id=message_id,
                account_id=account_id,
            )
