"""
Initial Indexer for MailVerdict.

Handles first-run indexing of historical mails into the vector store.
Uses folder special_use to determine ground truth spam labels:
- Mails in junk/spam folders -> is_spam=True
- All other mails -> is_spam=False

Configurable lookback via sync.lookback_days.
After initial indexing, incremental updates happen via the EmbeddingWorker.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from mail_verdict.database.models import Folder, Mail, SpecialUse
from mail_verdict.semantic.worker import EmbedItem, get_embedding_worker

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

_SPAM_FOLDER_TYPES = {SpecialUse.JUNK}


async def run_initial_index(
    db: DatabaseConnection,
    account_id: uuid.UUID,
    sync_settings: dict[str, Any],
    spam_settings: dict[str, Any],
) -> dict[str, int]:
    """
    Index historical mails for an account into the vector store.

    Loads all mails within the lookback window, determines ground truth
    spam labels from folder special_use, and enqueues them for embedding.

    Args:
        db: Database connection
        account_id: Account to index
        sync_settings: Sync settings dict (lookback_days)
        spam_settings: Spam settings dict (excerpt_length)

    Returns:
        Dict with counts: queued, skipped, total
    """
    worker = get_embedding_worker()
    account_id_str = str(account_id)

    lookback_days = int(sync_settings.get("lookback_days", 180))
    excerpt_length = int(spam_settings.get("excerpt_length", 300))

    # Determine lookback cutoff
    cutoff: datetime | None = None
    if lookback_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    # Load folders to build spam folder set
    async with db.session() as session:
        folder_result = await session.execute(select(Folder).where(Folder.account_id == account_id))
        folders = list(folder_result.scalars().all())

    spam_folder_ids = {f.id for f in folders if f.special_use in _SPAM_FOLDER_TYPES}

    folder_name_map = {f.id: f.imap_name for f in folders}

    logger.info(
        "Starting initial index for account %s: %d folders (%d spam), lookback=%s",
        account_id_str[:8],
        len(folders),
        len(spam_folder_ids),
        f"{lookback_days} days" if cutoff else "all",
    )

    # Load mails within lookback window
    async with db.session() as session:
        stmt = select(Mail).where(Mail.account_id == account_id)
        if cutoff is not None:
            stmt = stmt.where(Mail.received_at >= cutoff)
        stmt = stmt.order_by(Mail.received_at.asc())

        mail_result = await session.execute(stmt)
        mails = list(mail_result.scalars().all())

    total = len(mails)
    queued = 0
    skipped = 0

    for mail in mails:
        # Determine ground truth from folder
        is_spam = mail.folder_id in spam_folder_ids

        # Extract sender domain
        from_domain: str | None = None
        if mail.from_addr and "@" in mail.from_addr:
            from_domain = mail.from_addr.rsplit("@", 1)[-1].lower().strip(">")

        # Build embed item
        item = EmbedItem(
            mail_id=str(mail.id),
            account_id=account_id_str,
            from_addr=mail.from_addr,
            subject=mail.subject,
            body_text=mail.body_text,
            is_spam=is_spam,
            folder=folder_name_map.get(mail.folder_id),
            from_domain=from_domain,
            received_at=mail.received_at,
            excerpt_length=excerpt_length,
        )

        if await worker.enqueue(item):
            queued += 1
        else:
            skipped += 1

    logger.info(
        "Initial index for account %s: queued=%d, skipped=%d, total=%d",
        account_id_str[:8],
        queued,
        skipped,
        total,
    )

    return {"queued": queued, "skipped": skipped, "total": total}
