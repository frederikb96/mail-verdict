"""
Verdict Pipeline: orchestrates the spam detection flow for one message.

Steps:
1. Gate on folder type (skip sent/drafts/trash/junk/archive), draft flag,
   and per-account spam_enabled
2. Durability gate: skip if a verdict already exists for this message's
   (account_id, message_id_hdr) -- never reclassify, even across retention
   purge or a UIDVALIDITY resync
3. Extract an excerpt from the message
4. Call SpamAnalyst
5. Store the verdict in Postgres
6. If spam: move to the account's junk folder (+ optionally mark read)

The verdict is purely content-based, drawn from a short excerpt of the
message plus its envelope and auth signals -- no embeddings or vector
search involved.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from mail_verdict.database.models import Folder, Message, VerdictSource
from mail_verdict.postimap.actions import move_message
from mail_verdict.spam.analyst import AnalysisContext, SpamAnalyst

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.database.repository import (
        AccountPrefsRepository,
        FolderRepository,
        VerdictRepository,
    )
    from mail_verdict.settings.service import SettingsService

logger = logging.getLogger(__name__)

# Special-use folders where an AI verdict would be meaningless or wasteful:
# mail already left the inbox (sent/archived), never arrived as inbound mail
# (drafts), or is already classified (junk).
_SKIP_FOLDER_SPECIAL_USE = {"sent", "drafts", "trash", "junk", "archive"}


class VerdictPipeline:
    """Orchestrates spam detection for incoming messages."""

    def __init__(
        self,
        settings_service: SettingsService,
        analyst: SpamAnalyst,
        verdict_repo: VerdictRepository,
        folder_repo: FolderRepository,
        account_prefs_repo: AccountPrefsRepository,
        db: DatabaseConnection,
    ) -> None:
        """
        Initialize the verdict pipeline.

        Args:
            settings_service: Application settings service
            analyst: LLM spam analyst
            verdict_repo: Verdict persistence
            folder_repo: Folder repository, for junk-folder resolution
            account_prefs_repo: Account prefs, for the per-account spam_enabled gate
            db: Database connection for direct SQL updates
        """
        self._settings = settings_service
        self._analyst = analyst
        self._verdict_repo = verdict_repo
        self._folder_repo = folder_repo
        self._account_prefs_repo = account_prefs_repo
        self._db = db

    async def process_message(self, msg: Message, folder: Folder) -> bool | None:
        """
        Run the verdict pipeline for a single message.

        Returns None if processing was skipped (disabled, wrong folder,
        already classified). Returns True if classified spam, False if not.
        Errors are logged but never raised -- a pipeline failure must never
        block the caller (sync/rules dispatch).

        Args:
            msg: Message ORM object with full content
            folder: Folder the message currently resides in
        """
        spam_settings = self._settings.get("spam")
        if not spam_settings.get("enabled", False):
            return None

        account_prefs = await self._account_prefs_repo.get_by_account(msg.account_id)
        if account_prefs is None or not account_prefs.spam_enabled:
            logger.debug("Spam detection disabled for account %s, skipping", msg.account_id)
            return None

        effective_special_use = await self._folder_repo.get_effective_special_use(folder.id)
        if effective_special_use in _SKIP_FOLDER_SPECIAL_USE or msg.is_draft:
            logger.debug(
                "Skipping spam check for %s folder", effective_special_use or folder.imap_name,
            )
            return None

        msg_id_str = str(msg.id)

        if msg.message_id and await self._verdict_repo.has_ai_verdict_for_header(
            msg.account_id, msg.message_id,
        ):
            logger.debug("Message %s already has an AI verdict, skipping", msg_id_str[:8])
            return None

        excerpt_length = int(spam_settings.get("excerpt_length", 300))
        auto_move_to_junk = bool(spam_settings.get("auto_move_to_junk", True))
        auto_mark_read = bool(spam_settings.get("auto_mark_read", True))

        try:
            to_display = _format_addr_list(msg.to_addrs)
            body_excerpt = (msg.body_text or "")[:excerpt_length]
            if msg.is_truncated:
                body_excerpt = (
                    f"{body_excerpt}\n[message body was too large to fetch; "
                    f"classified on envelope only]"
                )

            headers_dict = msg.raw_headers if isinstance(msg.raw_headers, dict) else None
            context = AnalysisContext(
                mail_id=msg_id_str,
                from_addr=msg.from_addr,
                to_addrs=to_display,
                subject=msg.subject,
                body_excerpt=body_excerpt,
                dkim_pass=_extract_auth_signal(headers_dict, "dkim"),
                spf_pass=_extract_auth_signal(headers_dict, "spf"),
                dmarc_pass=_extract_auth_signal(headers_dict, "dmarc"),
            )

            verdict = await self._analyst.analyze(context)

            await self._verdict_repo.create_verdict(
                mail_id=msg.id,
                account_id=msg.account_id,
                is_spam=verdict.is_spam,
                source=VerdictSource.AI,
                message_id_hdr=msg.message_id,
                model_used=self._settings.get("ai").get("model"),
            )

            if verdict.is_spam and auto_move_to_junk:
                await self._move_to_junk(msg, folder, auto_mark_read)

            logger.info(
                "Verdict pipeline complete",
                extra={
                    "message_id": msg_id_str[:8],
                    "verdict": "spam" if verdict.is_spam else "not-spam",
                },
            )
            return verdict.is_spam

        except Exception:
            logger.exception("Verdict pipeline failed for message %s", msg_id_str[:8])
            return None

    async def _move_to_junk(
        self, msg: Message, source_folder: Folder, auto_mark_read: bool,
    ) -> None:
        """
        Move a spam-classified message to the account's junk folder.

        Args:
            msg: Message ORM object
            source_folder: Current folder
            auto_mark_read: Whether to mark the message as seen
        """
        junk_folder_id = await self._resolve_junk_folder(msg.account_id)
        if not junk_folder_id or junk_folder_id == source_folder.id:
            return

        async with self._db.session() as session:
            await move_message(session, msg.id, junk_folder_id)
            if auto_mark_read:
                from mail_verdict.postimap.actions import set_flags

                await set_flags(session, msg.id, is_seen=True)

    async def _resolve_junk_folder(self, account_id: uuid.UUID) -> uuid.UUID | None:
        """
        Resolve the junk folder UUID for an account by its effective special_use.

        Args:
            account_id: Account UUID

        Returns:
            Junk folder UUID or None if not found
        """
        return await self._folder_repo.resolve_special_folder(account_id, "junk")


def _format_addr_list(addrs: Any) -> str | None:
    """Render a jsonb address list as a short display string."""
    if isinstance(addrs, list) and addrs:
        return ", ".join(str(a) for a in addrs[:5])
    return None


def _extract_auth_signal(raw_headers: dict[str, Any] | None, protocol: str) -> bool | None:
    """
    Extract an authentication signal from raw headers.

    Looks for an Authentication-Results header and checks for pass/fail
    for the given protocol (dkim, spf, dmarc).

    Args:
        raw_headers: Raw message headers jsonb
        protocol: Protocol name to check (dkim, spf, dmarc)

    Returns:
        True if pass, False if fail, None if not found
    """
    if not raw_headers:
        return None

    auth_results = raw_headers.get("authentication-results", "")
    if not auth_results:
        return None

    auth_str = str(auth_results).lower()
    if f"{protocol}=pass" in auth_str:
        return True
    if f"{protocol}=fail" in auth_str or f"{protocol}=softfail" in auth_str:
        return False

    return None
