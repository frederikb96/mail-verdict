"""
MCP server tools for MailVerdict.

Provides FastMCP tools that read from Postgres (not IMAP directly).
Tools: search_mail, get_mail, list_folders, list_accounts, move_mail,
       tag_mail, get_verdict, get_stats.
"""

from __future__ import annotations

import uuid

from fastmcp import FastMCP

from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import (
    Account,
    Folder,
    Mail,
    TagSource,
)
from mail_verdict.database.repository import (
    FolderRepository,
    MailRepository,
    TagRepository,
    VerdictRepository,
)

mcp = FastMCP(
    name="mail-verdict",
    instructions="AI-powered email management. Search, read, move, and tag mail.",
)


@mcp.tool()
async def search_mail(
    query: str,
    account_id: str | None = None,
    mode: str = "fulltext",
    limit: int = 20,
) -> list[dict]:
    """
    Search emails by content.

    Args:
        query: Search query text
        account_id: Optional account UUID to scope search
        mode: Search mode - 'fulltext' (Postgres) or 'semantic' (Qdrant vectors)
        limit: Max results (default 20)

    Returns:
        List of matching mail summaries with id, subject, from, date
    """
    results: list[dict] = []
    db = get_db_connection()

    if mode == "semantic":
        from mail_verdict.semantic.store import get_semantic_store

        try:
            store = get_semantic_store()
            hits = await store.search(
                query,
                limit=limit,
                account_id=account_id,
            )
            for hit in hits:
                results.append(
                    {
                        "mail_id": hit.mail_id,
                        "score": hit.score,
                        "source": "semantic",
                    }
                )
        except RuntimeError:
            return [{"error": "SemanticStore not initialized"}]
    else:
        mail_repo = MailRepository(db)
        if account_id:
            mails = await mail_repo.search_fulltext(
                uuid.UUID(account_id),
                query,
                limit=limit,
            )
            for mail in mails:
                results.append(_mail_summary(mail))
        else:
            from sqlalchemy import select

            async with db.session() as session:
                accts = await session.execute(select(Account.id))
                for row in accts.all():
                    mails = await mail_repo.search_fulltext(
                        row[0],
                        query,
                        limit=limit,
                    )
                    for mail in mails:
                        results.append(_mail_summary(mail))

    return results[:limit]


@mcp.tool()
async def get_mail(
    mail_id: str,
    account_id: str,
) -> dict:
    """
    Get full email details by ID.

    Args:
        mail_id: Mail UUID
        account_id: Account UUID (for scoping)

    Returns:
        Full mail content including subject, body, headers, auth signals
    """
    db = get_db_connection()
    mail_repo = MailRepository(db)
    mail = await mail_repo.get_by_id(uuid.UUID(account_id), uuid.UUID(mail_id))
    if mail is None:
        return {"error": "Mail not found"}

    return {
        "id": str(mail.id),
        "subject": mail.subject,
        "from_addr": mail.from_addr,
        "to_addrs": mail.to_addrs,
        "cc_addrs": mail.cc_addrs,
        "body_text": mail.body_text,
        "received_at": mail.received_at.isoformat() if mail.received_at else None,
        "is_read": mail.is_read,
        "is_flagged": mail.is_flagged,
        "dkim_pass": mail.dkim_pass,
        "spf_pass": mail.spf_pass,
        "dmarc_pass": mail.dmarc_pass,
    }


@mcp.tool()
async def list_folders(
    account_id: str,
) -> list[dict]:
    """
    List all folders for an account.

    Args:
        account_id: Account UUID

    Returns:
        List of folders with name, special_use, last_synced
    """
    db = get_db_connection()
    folder_repo = FolderRepository(db)
    folders = await folder_repo.get_by_account(uuid.UUID(account_id))
    return [
        {
            "id": str(f.id),
            "imap_name": f.imap_name,
            "display_name": f.display_name,
            "special_use": f.special_use.value if f.special_use else None,
            "last_synced_at": f.last_synced_at.isoformat() if f.last_synced_at else None,
        }
        for f in folders
    ]


@mcp.tool()
async def list_accounts() -> list[dict]:
    """
    List all configured mail accounts.

    Returns:
        List of accounts with id, name, host, active status
    """
    from sqlalchemy import select

    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Account).order_by(Account.name))
        accounts = list(result.scalars().all())

    return [
        {
            "id": str(a.id),
            "name": a.name,
            "imap_host": a.imap_host,
            "is_active": a.is_active,
        }
        for a in accounts
    ]


@mcp.tool()
async def move_mail(
    mail_id: str,
    account_id: str,
    target_folder: str,
) -> dict:
    """
    Move a mail to a different folder (in database).

    Args:
        mail_id: Mail UUID to move
        account_id: Account UUID
        target_folder: Target folder IMAP name

    Returns:
        Success status and message
    """
    from sqlalchemy import select, update

    db = get_db_connection()
    mail_repo = MailRepository(db)
    mail = await mail_repo.get_by_id(uuid.UUID(account_id), uuid.UUID(mail_id))
    if mail is None:
        return {"success": False, "error": "Mail not found"}

    async with db.session() as session:
        result = await session.execute(
            select(Folder).where(
                Folder.account_id == uuid.UUID(account_id),
                Folder.imap_name == target_folder,
            )
        )
        folder = result.scalar_one_or_none()
        if folder is None:
            return {"success": False, "error": f"Folder not found: {target_folder}"}

        await session.execute(
            update(Mail).where(Mail.id == uuid.UUID(mail_id)).values(folder_id=folder.id)
        )

    return {"success": True, "message": f"Moved to {target_folder}"}


@mcp.tool()
async def tag_mail(
    mail_id: str,
    tag_name: str,
    source: str = "user",
) -> dict:
    """
    Add a tag to a mail.

    Args:
        mail_id: Mail UUID
        tag_name: Tag string to add
        source: Tag source (user, rule, enrichment, spam, imap)

    Returns:
        Success status
    """
    db = get_db_connection()
    tag_repo = TagRepository(db)

    source_map = {
        "user": TagSource.USER,
        "rule": TagSource.RULE,
        "enrichment": TagSource.ENRICHMENT,
        "spam": TagSource.SPAM,
        "imap": TagSource.IMAP,
    }
    tag_source = source_map.get(source, TagSource.USER)

    tag = await tag_repo.add_tag(uuid.UUID(mail_id), tag_name, tag_source)
    return {"success": True, "tag_name": tag.tag_name, "source": tag.source.value}


@mcp.tool()
async def get_verdict(
    mail_id: str,
) -> dict | None:
    """
    Get the latest spam verdict for a mail.

    Args:
        mail_id: Mail UUID

    Returns:
        Verdict details or None
    """
    db = get_db_connection()
    verdict_repo = VerdictRepository(db)
    verdict = await verdict_repo.get_latest_for_mail(uuid.UUID(mail_id))
    if verdict is None:
        return None

    return {
        "id": str(verdict.id),
        "mail_id": str(verdict.mail_id),
        "is_spam": verdict.is_spam,
        "model_used": verdict.model_used,
        "reasoning": verdict.reasoning,
        "source": verdict.source.value,
        "created_at": verdict.created_at.isoformat(),
    }


@mcp.tool()
async def get_stats(
    account_id: str | None = None,
) -> dict:
    """
    Get spam detection statistics.

    Args:
        account_id: Optional account UUID to scope stats

    Returns:
        Statistics including total verdicts, spam/ham counts, accuracy
    """
    from mail_verdict.spam.metrics import SpamMetrics

    db = get_db_connection()
    metrics = SpamMetrics(db)

    if account_id:
        stats = await metrics.get_stats(uuid.UUID(account_id))
        return {
            "total_verdicts": stats.total_verdicts,
            "spam_count": stats.spam_count,
            "ham_count": stats.ham_count,
            "false_positives": stats.false_positives,
            "false_negatives": stats.false_negatives,
            "accuracy": stats.accuracy,
            "fp_rate": stats.fp_rate,
            "fn_rate": stats.fn_rate,
        }

    # Aggregate across all accounts
    from sqlalchemy import select

    async with db.session() as session:
        result = await session.execute(select(Account.id))
        account_ids = [row[0] for row in result.all()]

    totals: dict[str, int | float] = {
        "total_verdicts": 0,
        "spam_count": 0,
        "ham_count": 0,
        "false_positives": 0,
        "false_negatives": 0,
    }
    accuracy_values: list[float] = []

    for aid in account_ids:
        stats = await metrics.get_stats(aid)
        totals["total_verdicts"] += stats.total_verdicts
        totals["spam_count"] += stats.spam_count
        totals["ham_count"] += stats.ham_count
        totals["false_positives"] += stats.false_positives
        totals["false_negatives"] += stats.false_negatives
        if stats.total_verdicts > 0:
            accuracy_values.append(stats.accuracy)

    totals["accuracy"] = sum(accuracy_values) / len(accuracy_values) if accuracy_values else 1.0
    return totals


def _mail_summary(mail: Mail) -> dict:
    """Convert a Mail model to a summary dict for MCP responses."""
    return {
        "id": str(mail.id),
        "subject": mail.subject,
        "from_addr": mail.from_addr,
        "received_at": mail.received_at.isoformat() if mail.received_at else None,
        "is_read": mail.is_read,
        "is_flagged": mail.is_flagged,
    }
