"""
Spam feedback listener.

Routes postimap_events message updates that moved a message to or from
the junk folder into SpamFeedbackHandler's contradiction check. New-mail
classification is no longer this module's concern -- that is
pipeline/enqueue.py's live arrival trigger and pipeline/runner.py's
classify stage; this listener only ever reacts to a folder move, never to
arrival, which is what keeps it from ever looping on the pipeline's own
writes (a move-spam effect's Move fires exactly this same update event,
and the contradiction check in spam/feedback.py is what makes that a
no-op rather than a spurious correction).

The "moved out of junk" signal only fires for a move made inside this
application. A move made in another mail client (Thunderbird, webmail) is
not a folder_id change at all from PostIMAP's side: IMAP assigns the
message a new UID in the destination and expunges it from the source, so
the mirror follows as an expunged_at update in the source folder plus a
separate insert in the destination -- no old_folder_id, no signal.
Correlating those two rows by message_id would catch it, but that is
cross-folder dedup, a documented non-goal PostIMAP deliberately does not
attempt (it would be wrong on a server that genuinely duplicates a
message across folders); not attempted here either for the same reason.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mail_verdict.database.repository import FolderRepository
    from mail_verdict.postimap.listener import PostimapEvent
    from mail_verdict.spam.feedback import SpamFeedbackHandler

logger = logging.getLogger(__name__)


class SpamFeedbackListener:
    """Reacts to a folder move that lands in or leaves the junk folder."""

    def __init__(self, feedback: SpamFeedbackHandler, folder_repo: FolderRepository) -> None:
        """
        Args:
            feedback: The contradiction-gated feedback handler
            folder_repo: Folder repository for special_use lookups
        """
        self._feedback = feedback
        self._folder_repo = folder_repo

    async def handle_message_event(self, event: PostimapEvent) -> None:
        """
        Handle a message event from postimap_events -- only a folder-move
        update is ever acted on.

        Args:
            event: Parsed postimap_events payload
        """
        if event.op != "update" or "folder_id" not in event.changed:
            return
        await self._handle_move(event)

    async def _handle_move(self, event: PostimapEvent) -> None:
        if not event.folder_id:
            return
        try:
            message_id = uuid.UUID(event.id)
            account_id = uuid.UUID(event.account_id)
            new_folder_id = uuid.UUID(event.folder_id)
        except ValueError:
            logger.warning("Invalid message update event payload: %s", event)
            return

        new_special_use = await self._folder_repo.get_effective_special_use(new_folder_id)

        if new_special_use == "junk":
            await self._feedback.handle_folder_move_to_junk(
                mail_id=message_id, account_id=account_id,
            )
            return

        if not event.old_folder_id:
            return
        try:
            old_folder_id = uuid.UUID(event.old_folder_id)
        except ValueError:
            logger.warning("Invalid old_folder_id in message update event: %s", event.old_folder_id)
            return
        old_special_use = await self._folder_repo.get_effective_special_use(old_folder_id)
        if old_special_use == "junk":
            await self._feedback.handle_folder_move_out_of_junk(
                mail_id=message_id, account_id=account_id,
                destination_special_use=new_special_use,
            )
