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
from typing import TYPE_CHECKING, Any

from mail_verdict.database.models import SpecialUse, VerdictSource
from mail_verdict.spam.analyst import AnalysisContext, NeighborContext, SpamAnalyst

if TYPE_CHECKING:
    from mail_verdict.config import MailVerdictConfig
    from mail_verdict.database.models import Folder, Mail
    from mail_verdict.database.repository import FolderRepository, MailRepository, VerdictRepository
    from mail_verdict.semantic.store import SemanticStore
    from mail_verdict.sync.actions import ActionPropagator

logger = logging.getLogger(__name__)

_SKIP_FOLDER_TYPES = {SpecialUse.SENT, SpecialUse.DRAFTS, SpecialUse.TRASH}


class VerdictPipeline:
    """
    Orchestrates spam detection for incoming mails.

    Integrates SemanticStore, SpamAnalyst, VerdictRepository,
    and ActionPropagator into a cohesive flow.
    """

    def __init__(
        self,
        config: MailVerdictConfig,
        semantic_store: SemanticStore,
        analyst: SpamAnalyst,
        verdict_repo: VerdictRepository,
        mail_repo: MailRepository,
        action_propagator: ActionPropagator | None = None,
        folder_repo: FolderRepository | None = None,
    ) -> None:
        """
        Initialize the verdict pipeline.

        Args:
            config: Global configuration
            semantic_store: Vector store for embeddings and search
            analyst: LLM spam analyst
            verdict_repo: Verdict persistence
            mail_repo: Mail persistence (for lookups)
            action_propagator: IMAP action executor (optional for testing)
            folder_repo: Folder repository for spam folder lookup
        """
        self._config = config
        self._store = semantic_store
        self._analyst = analyst
        self._verdict_repo = verdict_repo
        self._mail_repo = mail_repo
        self._action = action_propagator
        self._folder_repo = folder_repo

    async def process_mail(
        self,
        mail: Mail,
        folder: Folder,
    ) -> bool | None:
        """
        Run the full spam verdict pipeline for a single mail.

        Returns None if processing was skipped (disabled, wrong folder type).
        Returns True if mail was classified as spam, False if not-spam.
        Errors are logged but never raised (don't block sync).

        Args:
            mail: Mail ORM object with full content
            folder: Folder the mail resides in
        """
        if not self._config.spam.enabled:
            return None

        if folder.special_use in _SKIP_FOLDER_TYPES:
            logger.debug(
                "Skipping spam check for %s folder",
                folder.special_use.value if folder.special_use else folder.imap_name,
            )
            return None

        mail_id_str = str(mail.id)
        account_id_str = str(mail.account_id)
        spam_config = self._config.spam

        try:
            # Step 1: Build embedding text and embed
            from mail_verdict.semantic.store import SemanticStore as SS

            embedding_text = SS.build_embedding_text(
                from_addr=mail.from_addr,
                subject=mail.subject,
                body_text=mail.body_text,
                excerpt_length=spam_config.excerpt_length,
            )

            if not embedding_text.strip():
                logger.debug("Empty embedding text for mail %s, skipping", mail_id_str[:8])
                return None

            # Extract sender domain
            from_domain: str | None = None
            if mail.from_addr and "@" in mail.from_addr:
                from_domain = mail.from_addr.rsplit("@", 1)[-1].lower().strip(">")

            # Step 2: Embed the mail
            embed_ok = await self._store.upsert(
                mail_id=mail_id_str,
                text=embedding_text,
                account_id=account_id_str,
                folder=folder.imap_name,
                from_domain=from_domain,
                received_at=mail.received_at,
            )

            if not embed_ok:
                logger.warning("Failed to embed mail %s, skipping spam check", mail_id_str[:8])
                return None

            # Step 3: Find neighbors
            neighbors = await self._store.search(
                query_text=embedding_text,
                limit=spam_config.neighbor_count,
                account_id=account_id_str,
                exclude_ids=[mail_id_str],
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

            # Build to_addrs display string
            to_display: str | None = None
            if mail.to_addrs and isinstance(mail.to_addrs, dict):
                addrs = mail.to_addrs.get("addrs", [])
                if isinstance(addrs, list):
                    to_display = ", ".join(str(a) for a in addrs[:5])

            body_excerpt = (mail.body_text or "")[: spam_config.excerpt_length]

            context = AnalysisContext(
                mail_id=mail_id_str,
                from_addr=mail.from_addr,
                to_addrs=to_display,
                subject=mail.subject,
                body_excerpt=body_excerpt,
                dkim_pass=mail.dkim_pass,
                spf_pass=mail.spf_pass,
                dmarc_pass=mail.dmarc_pass,
                neighbors=neighbor_contexts,
            )

            # Step 5: Call spam analyst
            verdict = await self._analyst.analyze(context)

            # Step 6: Store verdict in Postgres
            neighbor_id_list = [n.mail_id for n in neighbors]
            await self._verdict_repo.create_verdict(
                mail_id=mail.id,
                is_spam=verdict.is_spam,
                source=VerdictSource.AI,
                model_used=self._config.ai.model,
                reasoning=None,
                neighbor_ids=neighbor_id_list if neighbor_id_list else None,
            )

            # Step 7: Update Qdrant payload with verdict tag (no re-embedding)
            await self._store.update_payload(
                mail_id=mail_id_str,
                payload={"is_spam": str(verdict.is_spam).lower()},
            )

            # Step 8: If spam, move to spam folder and optionally mark read
            if verdict.is_spam and self._action:
                spam_folder = await self._find_spam_folder_name(mail.account_id)
                if spam_folder:
                    await self._action.move_to_spam(
                        folder=folder.imap_name,
                        uid_set=str(mail.uid),
                        spam_folder=spam_folder,
                        mark_read=spam_config.auto_mark_read,
                    )

            logger.info(
                "Verdict pipeline complete",
                extra={
                    "mail_id": mail_id_str[:8],
                    "verdict": "spam" if verdict.is_spam else "not-spam",
                    "neighbors": len(neighbors),
                },
            )

            return verdict.is_spam

        except Exception:
            logger.exception(
                "Verdict pipeline failed for mail %s",
                mail_id_str[:8],
            )
            return None

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

    _SPAM_FOLDER_FALLBACKS = ["Junk", "Junk Mail", "Spam", "Bulk Mail"]

    async def _find_spam_folder_name(self, account_id: Any) -> str | None:
        """
        Look up the actual spam/junk folder for an account.

        Queries FolderRepository for a folder with special_use=JUNK.
        Falls back to common conventional names.
        """
        import uuid as _uuid

        if self._folder_repo and account_id:
            account_uuid = (
                account_id if isinstance(account_id, _uuid.UUID) else _uuid.UUID(str(account_id))
            )
            folders = await self._folder_repo.get_by_account(account_uuid)
            for f in folders:
                if f.special_use and f.special_use.value == "junk":
                    return f.imap_name
            # Fallback: check by conventional name
            folder_names = {f.imap_name for f in folders}
            for name in self._SPAM_FOLDER_FALLBACKS:
                if name in folder_names:
                    return name

        return self._SPAM_FOLDER_FALLBACKS[0]
