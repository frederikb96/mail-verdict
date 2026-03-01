"""
Spam event processor.

Consumes events from SyncManager event queues, routes them
to VerdictPipeline (for new mails) and SpamFeedbackHandler
(for user folder moves). Runs as a background asyncio task
per account.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from mail_verdict.database.models import SpecialUse
from mail_verdict.sync.events import (
    MailMoved,
    MailReceived,
    MailSpamDetected,
    SyncEvent,
)

if TYPE_CHECKING:
    from mail_verdict.database.repository import FolderRepository, MailRepository
    from mail_verdict.spam.feedback import SpamFeedbackHandler
    from mail_verdict.spam.pipeline import VerdictPipeline

logger = logging.getLogger(__name__)


class SpamEventProcessor:
    """
    Background processor consuming sync events for spam analysis.

    Subscribes to SyncManager event queues and routes events
    to the appropriate handler:
    - MailReceived -> VerdictPipeline.process_mail()
    - MailSpamDetected -> SpamFeedbackHandler.handle_moved_to_spam()
    - MailMoved (from spam) -> SpamFeedbackHandler.handle_moved_from_spam()
    """

    def __init__(
        self,
        pipeline: VerdictPipeline,
        feedback: SpamFeedbackHandler,
        mail_repo: MailRepository,
        folder_repo: FolderRepository,
    ) -> None:
        """
        Initialize the spam event processor.

        Args:
            pipeline: VerdictPipeline for new mail analysis
            feedback: SpamFeedbackHandler for user corrections
            mail_repo: Mail repository for lookups
            folder_repo: Folder repository for lookups
        """
        self._pipeline = pipeline
        self._feedback = feedback
        self._mail_repo = mail_repo
        self._folder_repo = folder_repo
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running = False

    async def start(self, event_queues: dict[str, asyncio.Queue[SyncEvent]]) -> None:
        """
        Start processing events from the given queues.

        Args:
            event_queues: Mapping of account_name -> event queue
        """
        self._running = True
        for name, queue in event_queues.items():
            self._tasks[name] = asyncio.create_task(
                self._process_loop(name, queue),
                name=f"spam-processor-{name}",
            )
        logger.info("Spam event processor started for %d accounts", len(event_queues))

    async def stop(self) -> None:
        """Stop all processing loops."""
        self._running = False
        for task in self._tasks.values():
            task.cancel()
        for task in self._tasks.values():
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        logger.info("Spam event processor stopped")

    async def _process_loop(
        self,
        account_name: str,
        queue: asyncio.Queue[SyncEvent],
    ) -> None:
        """
        Process events from a single account's queue.

        Args:
            account_name: Account identifier for logging
            queue: Event queue to consume from
        """
        logger.info("Spam processor started for account %s", account_name)

        while self._running:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=60.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                await self._handle_event(event, account_name)
            except Exception:
                logger.exception(
                    "Spam processor error handling event",
                    extra={"account": account_name, "event_type": type(event).__name__},
                )

    async def _handle_event(
        self,
        event: SyncEvent,
        account_name: str,
    ) -> None:
        """
        Route a single event to the appropriate handler.

        Args:
            event: Sync event to process
            account_name: Account name for logging
        """
        if isinstance(event, MailReceived):
            await self._handle_mail_received(event)

        elif isinstance(event, MailSpamDetected):
            await self._handle_spam_detected(event)

        elif isinstance(event, MailMoved):
            await self._handle_mail_moved(event)

    async def _handle_mail_received(self, event: MailReceived) -> None:
        """
        Handle new mail: look up full mail + folder, run verdict pipeline.

        Args:
            event: MailReceived event
        """
        mail = await self._mail_repo.get_by_folder_and_uid(event.folder_id, event.uid)
        if mail is None:
            logger.debug("Mail uid=%d not found in folder for spam check", event.uid)
            return

        # Look up folder
        folders = await self._folder_repo.get_by_account(event.account_id)
        folder = None
        for f in folders:
            if f.id == event.folder_id:
                folder = f
                break

        if folder is None:
            return

        await self._pipeline.process_mail(mail, folder)

    async def _handle_spam_detected(self, event: MailSpamDetected) -> None:
        """
        Handle mail moved to spam by user.

        Args:
            event: MailSpamDetected event
        """
        if not event.message_id:
            return

        mails = await self._mail_repo.get_by_message_id(event.account_id, event.message_id)
        if mails:
            await self._feedback.handle_moved_to_spam(
                mail_id=mails[0].id,
                account_id=event.account_id,
            )

    async def _handle_mail_moved(self, event: MailMoved) -> None:
        """
        Handle mail moved between folders.

        If moved FROM a spam folder, record as user correction (not-spam).

        Args:
            event: MailMoved event
        """
        if not event.from_folder_id or not event.message_id:
            return

        # Check if source folder was spam
        folders = await self._folder_repo.get_by_account(event.account_id)
        from_folder = None
        for f in folders:
            if f.id == event.from_folder_id:
                from_folder = f
                break

        if from_folder is None or from_folder.special_use != SpecialUse.JUNK:
            return

        mails = await self._mail_repo.get_by_message_id(event.account_id, event.message_id)
        if mails:
            await self._feedback.handle_moved_from_spam(
                mail_id=mails[0].id,
                account_id=event.account_id,
            )
