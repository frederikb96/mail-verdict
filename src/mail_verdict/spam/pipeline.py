"""
Verdict Pipeline: orchestrates the full spam detection flow.

Steps:
1. Check folder type (skip sent/drafts/trash)
2. Extract excerpt from mail
3. Embed mail via SemanticStore
4. Find neighbors via SemanticStore.search
5. Assemble context (mail + neighbors + auth signals)
6. Call SpamAnalyst
7. Store verdict in Postgres
8. If spam: move to spam folder + mark read
9. Update Qdrant payload with verdict tag
10. Emit event if spam
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from mail_verdict.database.models import (
    AccountPrefs,
    Folder,
    Message,
    VerdictSource,
)
from mail_verdict.spam.analyst import AnalysisContext, NeighborContext, SpamAnalyst

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.database.repository import (
        FolderRepository,
        MessageRepository,
        VerdictRepository,
    )
    from mail_verdict.semantic.store import SemanticStore
    from mail_verdict.settings.service import SettingsService

logger = logging.getLogger(__name__)

_SKIP_FOLDER_TYPES = {"sent", "drafts", "trash"}


class VerdictPipeline:
    """
    Orchestrates spam detection for incoming messages.

    Integrates SemanticStore, SpamAnalyst, VerdictRepository,
    and direct DB updates into a cohesive flow.
    """

    def __init__(
        self,
        settings_service: SettingsService,
        semantic_store: SemanticStore,
        analyst: SpamAnalyst,
        verdict_repo: VerdictRepository,
        message_repo: MessageRepository,
        folder_repo: FolderRepository | None = None,
        db: DatabaseConnection | None = None,
    ) -> None:
        """
        Initialize the verdict pipeline.

        Args:
            settings_service: Application settings service
            semantic_store: Vector store for embeddings and search
            analyst: LLM spam analyst
            verdict_repo: Verdict persistence
            message_repo: Message persistence (for lookups)
            folder_repo: Folder repository for spam folder lookup
            db: Database connection for direct SQL updates
        """
        self._settings = settings_service
        self._store = semantic_store
        self._analyst = analyst
        self._verdict_repo = verdict_repo
        self._message_repo = message_repo
        self._folder_repo = folder_repo
        self._db = db

    async def process_message(
        self,
        msg: Message,
        folder: Folder,
    ) -> bool | None:
        """
        Run the full spam verdict pipeline for a single message.

        Returns None if processing was skipped (disabled, wrong folder type).
        Returns True if message was classified as spam, False if not-spam.
        Errors are logged but never raised (don't block sync).

        Args:
            msg: Message ORM object with full content
            folder: Folder the message resides in
        """
        spam_settings = self._settings.get("spam")
        if not spam_settings.get("enabled", False):
            return None

        if folder.special_use in _SKIP_FOLDER_TYPES:
            logger.debug(
                "Skipping spam check for %s folder",
                folder.special_use or folder.imap_name,
            )
            return None

        msg_id_str = str(msg.id)
        account_id_str = str(msg.account_id)
        excerpt_length = int(spam_settings.get("excerpt_length", 300))
        neighbor_count = int(spam_settings.get("neighbor_count", 3))
        auto_mark_read = bool(spam_settings.get("auto_mark_read", True))

        try:
            # Step 1: Build embedding text and embed
            from mail_verdict.semantic.store import SemanticStore as SS

            embedding_text = SS.build_embedding_text(
                from_addr=msg.from_addr,
                subject=msg.subject,
                body_text=msg.body_text,
                excerpt_length=excerpt_length,
            )

            if not embedding_text.strip():
                logger.debug("Empty embedding text for message %s, skipping", msg_id_str[:8])
                return None

            # Extract sender domain
            from_domain: str | None = None
            if msg.from_addr and "@" in msg.from_addr:
                from_domain = msg.from_addr.rsplit("@", 1)[-1].lower().strip(">")

            # Step 2: Embed the message
            embed_ok = await self._store.upsert(
                mail_id=msg_id_str,
                text=embedding_text,
                account_id=account_id_str,
                folder=folder.imap_name,
                from_domain=from_domain,
                received_at=msg.received_at,
            )

            if not embed_ok:
                logger.warning("Failed to embed message %s, skipping spam check", msg_id_str[:8])
                return None

            # Step 3: Find neighbors
            neighbors = await self._store.search(
                query_text=embedding_text,
                limit=neighbor_count,
                account_id=account_id_str,
                exclude_ids=[msg_id_str],
            )

            # Step 4: Build analysis context
            neighbor_contexts = [
                NeighborContext(
                    mail_id=n.mail_id,
                    tag=n.payload.get("is_spam"),
                    excerpt=self._get_neighbor_excerpt(n.payload),
                )
                for n in neighbors
            ]

            # Build to_addrs display string (handles flat list + legacy dict)
            to_display: str | None = None
            if msg.to_addrs:
                addrs = msg.to_addrs
                if isinstance(addrs, dict):
                    addrs = addrs.get("addrs", [])
                if isinstance(addrs, list):
                    to_display = ", ".join(str(a) for a in addrs[:5])

            body_excerpt = (msg.body_text or "")[:excerpt_length]

            # Extract auth signals from raw_headers if available
            dkim_pass = _extract_auth_signal(msg.raw_headers, "dkim")
            spf_pass = _extract_auth_signal(msg.raw_headers, "spf")
            dmarc_pass = _extract_auth_signal(msg.raw_headers, "dmarc")

            context = AnalysisContext(
                mail_id=msg_id_str,
                from_addr=msg.from_addr,
                to_addrs=to_display,
                subject=msg.subject,
                body_excerpt=body_excerpt,
                dkim_pass=dkim_pass,
                spf_pass=spf_pass,
                dmarc_pass=dmarc_pass,
                neighbors=neighbor_contexts,
            )

            # Step 5: Call spam analyst
            verdict = await self._analyst.analyze(context)

            # Step 6: Store verdict in Postgres
            neighbor_id_list = [n.mail_id for n in neighbors]
            await self._verdict_repo.create_verdict(
                mail_id=msg.id,
                is_spam=verdict.is_spam,
                source=VerdictSource.AI,
                model_used=self._settings.get("ai").get("model", "gpt-5-mini"),
                reasoning=None,
                neighbor_ids=neighbor_id_list if neighbor_id_list else None,
            )

            # Step 7: Update Qdrant payload with verdict tag (no re-embedding)
            await self._store.update_payload(
                mail_id=msg_id_str,
                payload={"is_spam": str(verdict.is_spam).lower()},
            )

            # Step 8: If spam, move to spam folder via direct SQL UPDATE
            if verdict.is_spam:
                await self._move_to_spam(
                    msg=msg,
                    source_folder=folder,
                    auto_mark_read=auto_mark_read,
                )

            logger.info(
                "Verdict pipeline complete",
                extra={
                    "message_id": msg_id_str[:8],
                    "verdict": "spam" if verdict.is_spam else "not-spam",
                    "neighbors": len(neighbors),
                },
            )

            return verdict.is_spam

        except Exception:
            logger.exception(
                "Verdict pipeline failed for message %s",
                msg_id_str[:8],
            )
            return None

    async def _move_to_spam(
        self,
        msg: Message,
        source_folder: Folder,
        auto_mark_read: bool,
    ) -> None:
        """
        Move a spam-classified message to the junk folder via direct SQL UPDATE.

        PostIMAP's PG triggers handle outbound IMAP propagation automatically.

        Args:
            msg: Message ORM object
            source_folder: Current folder
            auto_mark_read: Whether to mark the message as seen
        """
        spam_folder_id = await self._resolve_junk_folder(msg.account_id)
        if not spam_folder_id or spam_folder_id == source_folder.id:
            return

        if not self._db:
            logger.warning("No DB connection for spam move, skipping")
            return

        update_values: dict[str, Any] = {"folder_id": spam_folder_id}
        if auto_mark_read:
            update_values["is_seen"] = True

        async with self._db.session() as session:
            await session.execute(
                update(Message)
                .where(Message.id == msg.id)
                .values(**update_values)
            )

    async def _resolve_junk_folder(self, account_id: uuid.UUID) -> uuid.UUID | None:
        """
        Resolve the junk/spam folder UUID for an account.

        Checks AccountPrefs.folder_mapping first, falls back to
        Folder.special_use == 'junk'.

        Args:
            account_id: Account UUID

        Returns:
            Junk folder UUID or None if not found
        """
        if not self._db:
            return None

        async with self._db.session() as session:
            # Try AccountPrefs folder_mapping first
            result = await session.execute(
                select(AccountPrefs.folder_mapping)
                .where(AccountPrefs.account_id == account_id)
            )
            mapping = result.scalar_one_or_none()
            if mapping and "junk" in mapping:
                folder_id_str = mapping["junk"]
                if folder_id_str:
                    try:
                        return uuid.UUID(folder_id_str)
                    except (ValueError, TypeError):
                        pass

            # Fall back to special_use detection
            result = await session.execute(
                select(Folder.id)
                .where(
                    Folder.account_id == account_id,
                    Folder.special_use == "junk",
                )
                .limit(1)
            )
            return result.scalar_one_or_none()

    def _get_neighbor_excerpt(self, payload: dict[str, Any]) -> str:
        """
        Extract a readable excerpt from a neighbor's Qdrant payload.

        Args:
            payload: Qdrant point payload
        """
        parts: list[str] = []
        if "from_domain" in payload:
            parts.append(f"from: {payload['from_domain']}")
        if "folder" in payload:
            parts.append(f"folder: {payload['folder']}")
        return " | ".join(parts) if parts else "(no details)"


def _extract_auth_signal(
    raw_headers: dict[str, Any] | None,
    protocol: str,
) -> bool | None:
    """
    Extract an authentication signal from raw headers.

    Looks for Authentication-Results header and checks for pass/fail
    for the given protocol (dkim, spf, dmarc).

    Args:
        raw_headers: Raw message headers JSONB
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
