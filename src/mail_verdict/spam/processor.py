"""
Spam event processor.

Routes postimap_events message events to VerdictPipeline (new mail) and
SpamFeedbackHandler (user folder moves to/from junk). Backfill suppression
is handled upstream by PostIMAP itself -- per-message insert events never
fire during a folder's initial sync, so no additional gating is needed here
beyond the pipeline's own durability gate.

The "moved out of junk" signal in _handle_possible_move only fires for a
move made inside this application. A move made in another mail client
(Thunderbird, webmail) is not a folder_id change at all from PostIMAP's
side: IMAP assigns the message a new UID in the destination and expunges
it from the source, so the mirror follows as an expunged_at update in the
source folder plus a separate insert in the destination -- no old_folder_id,
no signal. Correlating those two rows by message_id would catch it, but
that is cross-folder dedup, a documented non-goal PostIMAP deliberately
does not attempt (it would be wrong on a server that genuinely duplicates
a message across folders); not attempted here either for the same reason.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.database.repository import FolderRepository, MessageRepository
    from mail_verdict.postimap.listener import PostimapEvent
    from mail_verdict.spam.feedback import SpamFeedbackHandler
    from mail_verdict.spam.pipeline import VerdictPipeline

logger = logging.getLogger(__name__)


class SpamEventProcessor:
    """Processes postimap_events message events for spam analysis."""

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

    async def handle_message_event(self, event: PostimapEvent) -> None:
        """
        Handle a message event from postimap_events.

        Args:
            event: Parsed postimap_events payload
        """
        if event.op == "insert" and event.origin == "sync":
            await self._handle_new_message(event)
        elif event.op == "update" and "imap_uid" not in event.changed:
            # A folder_id-changing update surfaces as imap_uid also
            # changing (moves always reset it to NULL); an update that
            # left imap_uid untouched cannot be a folder move.
            return
        elif event.op == "update":
            await self._handle_possible_move(event)

    async def _handle_new_message(self, event: PostimapEvent) -> None:
        """Look up the full message + folder and run the verdict pipeline."""
        try:
            message_id = uuid.UUID(event.id)
            account_id = uuid.UUID(event.account_id)
            folder_id = uuid.UUID(event.folder_id) if event.folder_id else None
        except ValueError:
            logger.warning("Invalid message event payload: %s", event)
            return
        if folder_id is None:
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

    async def _handle_possible_move(self, event: PostimapEvent) -> None:
        """Check whether an update moved a message to/from the junk folder."""
        if not event.folder_id:
            return
        try:
            message_id = uuid.UUID(event.id)
            account_id = uuid.UUID(event.account_id)
            new_folder_id = uuid.UUID(event.folder_id)
        except ValueError:
            logger.warning("Invalid message update event payload: %s", event)
            return

        new_folder = await self._folder_repo.get_by_id(new_folder_id)
        if new_folder is None:
            return

        if new_folder.special_use == "junk":
            await self._feedback.handle_moved_to_spam(
                mail_id=message_id, account_id=account_id,
            )
        elif event.old_folder_id:
            await self._handle_possible_junk_exit(event.old_folder_id, message_id, account_id)

    async def _handle_possible_junk_exit(
        self, old_folder_id: str, message_id: uuid.UUID, account_id: uuid.UUID,
    ) -> None:
        """Record a correction when a move's source folder was junk and its destination isn't.

        old_folder_id is only present on a move made inside this application
        -- see the module docstring for the case it does not cover.
        """
        try:
            old_folder = await self._folder_repo.get_by_id(uuid.UUID(old_folder_id))
        except ValueError:
            logger.warning("Invalid old_folder_id in message update event: %s", old_folder_id)
            return

        if old_folder is not None and old_folder.special_use == "junk":
            await self._feedback.handle_moved_from_spam(
                mail_id=message_id, account_id=account_id,
            )
