"""
Spam feedback handler.

`apply_human_ruling` is the one place a person's ruling on a message is
recorded and applied: it writes the correction unconditionally, resolves
and moves to whichever folder the ruling implies, and announces the
change over the event ring -- whatever route the ruling arrived by, a
reading-pane thumb, a row's own Spam/Not-spam button, the review screen,
or an MCP tool call. One call does all of it; nothing else needs to be
paired with it.

`handle_folder_move_to_junk` / `handle_folder_move_out_of_junk` are a
different thing in kind: not a caller stating a ruling, but this
module's own detector of a move that already happened, from anywhere --
including a third-party mail client this application never sees a
request from. Gated on whether the move contradicts an *existing*
verdict, since that is stateless and restart-safe. The discriminator is
deliberately not "who wrote the row": both the pipeline's own move-spam
stage and a user dragging a message are the same application, and
`origin` cannot tell them apart (it distinguishes PostIMAP from
consumers, not the classifier from the user). Contradiction with the
stored verdict is something the classifier can never do to what it just
wrote in the same run: by the time a move-spam effect's own move
commits, its RecordVerdict effect already has, so the verdict this check
reads back already agrees -- and the same holds for a move this module's
own apply_human_ruling just made, which is what keeps the two from
double-writing when the listener later sees that same move go by.
A move with no verdict to contradict never records feedback, even
though that also gives up capturing a genuine human drag of never-
classified mail: any other match stage with a move-to-junk effect --
"block this sender" is the obvious one -- can produce that same
no-verdict move with nothing human about it, and there is no signal
here that tells the two apart, so treating the absent case as
"must be human" is a guess this module has no way to stand behind.
Moving spam to trash is excluded on purpose: deleting a message already
agreed to be spam is the ordinary outcome of a junk folder, not a
correction.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from mail_verdict.database.models import VerdictSource
from mail_verdict.database.repository import FolderRepository
from mail_verdict.postimap.actions import move_message

if TYPE_CHECKING:
    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.database.repository import VerdictRepository

logger = logging.getLogger(__name__)


class FolderResolutionError(Exception):
    """apply_human_ruling's ruling requires moving the message to a
    special-use folder (junk or inbox) this account doesn't have. The
    verdict itself is still recorded by the time this is raised -- only
    the move failed -- the same distinction archive and trash already
    make between "the ruling happened" and "the folder to put it in
    exists"."""

    def __init__(self, role: str, account_id: uuid.UUID) -> None:
        self.role = role
        self.account_id = account_id
        super().__init__(f"No {role} folder found for this account")


class SpamFeedbackHandler:
    """Records user_feedback verdict rows -- from an explicit ruling
    (recorded and moved together), and from folder moves that contradict
    the current verdict (recorded only, the move having already happened)."""

    def __init__(
        self,
        db: DatabaseConnection,
        verdict_repo: VerdictRepository,
        event_ring: EventRing | None = None,
    ) -> None:
        """
        Args:
            db: Session factory for apply_human_ruling's own move
            verdict_repo: Verdict persistence and the current-verdict read
            event_ring: Where apply_human_ruling announces verdict.issued
                -- None in a context with no live clients to reach (a
                test, a one-off script)
        """
        self._db = db
        self._verdict_repo = verdict_repo
        self._event_ring = event_ring

    async def apply_human_ruling(
        self, mail_id: uuid.UUID, account_id: uuid.UUID, *, is_spam: bool,
    ) -> bool:
        """
        Record an explicit ruling and move the message to match.

        Unconditional, like the folder-move listener's contradiction-gated
        pair is not: an explicit ruling is recorded whatever the current
        verdict already says. The move that follows is what the listener
        below will see go by; since the verdict it reads back afterward
        already agrees, its own contradiction gate does not write a second
        row for the same ruling.

        Whether -- and where -- the message moves depends on what the
        ruling changes, not only on the ruling itself: a spam ruling
        always moves to Junk, confirming one that already said spam
        exactly as much as reversing one that said clean. A not-spam
        ruling only moves the message when it reverses an existing spam
        verdict (back to the inbox -- this application has no record of
        where a message came from, so a rescue always lands there);
        confirming a verdict that already said not-spam moves nothing,
        since there is nowhere to rescue it from.

        Also the one place a ruling is announced: verdict.issued reaches
        every open page over the event ring the moment the verdict row
        is written, whatever surface this was called from -- a reading-
        pane thumb, a row's own button, a bulk action, or an MCP tool
        call. Announcing here rather than in each caller is what keeps
        the other three surfaces from silently disagreeing with an open
        page's stale badge, the way they used to.

        Args:
            mail_id: Mail UUID
            account_id: Account UUID
            is_spam: The ruling

        Returns:
            True if the verdict was recorded successfully. A message
            already in the target folder is left untouched by the move
            itself -- move_message's own idempotence, not reflected here.

        Raises:
            FolderResolutionError: The ruling requires a move (to junk,
                or back to the inbox) and this account has no such
                folder. The verdict itself is still recorded and
                announced by the time this raises -- only the move
                didn't happen.
        """
        prior = await self._verdict_repo.get_current_verdict(mail_id)
        ok = await self._record_feedback(mail_id, account_id, is_spam=is_spam)

        if ok and self._event_ring is not None:
            await self._event_ring.add(
                account_id, "verdict.issued",
                {
                    "message_id": str(mail_id), "is_spam": is_spam,
                    "source": "user_feedback", "account_id": str(account_id),
                },
            )

        if is_spam:
            role = "junk"
        elif prior is not None and prior.is_spam:
            role = "inbox"
        else:
            role = None

        if role is not None:
            folder_id = await FolderRepository(self._db).resolve_special_folder(account_id, role)
            if folder_id is None:
                raise FolderResolutionError(role, account_id)
            async with self._db.session() as session:
                await move_message(session, mail_id, folder_id)
        return ok

    async def handle_folder_move_to_junk(self, mail_id: uuid.UUID, account_id: uuid.UUID) -> bool:
        """
        A message landed in the junk folder. Records a correction only if
        an existing verdict says not-spam. No verdict at all -- whether
        because nothing has classified this message yet, or because
        another stage's own move-to-junk effect just fired with no
        RecordVerdict of its own -- produces no feedback row either, since
        there is nothing here to disagree with. The pipeline's own
        move-spam effect having just written a spam verdict is the same
        "already agrees" case as a verdict a person set earlier.

        Args:
            mail_id: Mail UUID
            account_id: Account UUID

        Returns:
            True if a correction was recorded
        """
        current = await self._verdict_repo.get_current_verdict(mail_id)
        if current is None or current.is_spam:
            return False
        return await self._record_feedback(mail_id, account_id, is_spam=True)

    async def handle_folder_move_out_of_junk(
        self, mail_id: uuid.UUID, account_id: uuid.UUID, *, destination_special_use: str | None,
    ) -> bool:
        """
        A message left the junk folder. Records a correction only if an
        existing verdict says spam and the destination is not trash --
        deleting mail already agreed to be spam is the commonest thing
        anyone does in a junk folder, not a sign the classifier was wrong.
        No verdict at all produces no feedback row, for the same reason
        as handle_folder_move_to_junk.

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
        if current is None or not current.is_spam:
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
