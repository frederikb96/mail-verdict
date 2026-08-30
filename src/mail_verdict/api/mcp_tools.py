"""
MCP server tools for MailVerdict.

Reads from Postgres, writes through postimap/actions.py -- never touches
IMAP/SMTP directly. Tools: search_mail, list_mails, get_mail, get_thread,
list_folders, list_accounts, move_mail, mark_mail, tag_mail, get_verdict,
submit_spam_feedback, send_mail, draft_mail, get_stats,
semantic_search_mail, get_semantic_status.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastmcp import FastMCP
from sqlalchemy import desc, select

from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Account, Folder, Message, TagSource
from mail_verdict.database.repository import (
    FolderRepository,
    MessageRepository,
    TagRepository,
    VerdictRepository,
)
from mail_verdict.postimap.actions import insert_outbox, move_message, set_flags

mcp = FastMCP(
    name="mail-verdict",
    instructions=(
        "AI-powered email management. Search, read, move, tag and send mail across "
        "one or more IMAP accounts; inspect and correct spam verdicts."
    ),
)


def _message_summary(msg: Message) -> dict[str, Any]:
    """Convert a Message model to a summary dict for MCP responses."""
    return {
        "id": str(msg.id),
        "account_id": str(msg.account_id),
        "folder_id": str(msg.folder_id),
        "thread_id": str(msg.thread_id),
        "subject": msg.subject,
        "from_addr": msg.from_addr,
        "received_at": msg.received_at.isoformat() if msg.received_at else None,
        "is_seen": msg.is_seen,
        "is_flagged": msg.is_flagged,
        "is_truncated": msg.is_truncated,
        "pending_sync": msg.imap_uid is None,
    }


@mcp.tool(
    name="search_mail",
    annotations={
        "title": "Search Mail",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def search_mail(
    query: str,
    account_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Search emails by full-text content match (subject, sender, body).

    Args:
        query: Search query text
        account_id: Optional account UUID to scope the search to one account
        limit: Max results, 1-100 (default 20)

    Returns:
        List of message summaries with id, account_id, subject, from_addr,
        received_at, is_seen, is_flagged, ranked by relevance
    """
    db = get_db_connection()
    msg_repo = MessageRepository(db)
    aid = uuid.UUID(account_id) if account_id else None
    rows = await msg_repo.search_fulltext_with_snippet(aid, query, limit=limit)
    return [{**_message_summary(msg), "snippet": snippet} for msg, snippet in rows]


@mcp.tool(
    name="list_mails",
    annotations={
        "title": "List Mails",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def list_mails(
    account_id: str,
    folder_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    List recent emails in an account, newest first.

    Args:
        account_id: Account UUID to list mail from
        folder_id: Optional folder UUID to scope to one folder
        limit: Max results, 1-100 (default 20)

    Returns:
        List of message summaries, newest received_at first
    """
    db = get_db_connection()
    async with db.session() as session:
        stmt = (
            select(Message)
            .where(Message.account_id == uuid.UUID(account_id), Message.expunged_at.is_(None))
            .order_by(desc(Message.received_at))
            .limit(min(limit, 100))
        )
        if folder_id:
            stmt = stmt.where(Message.folder_id == uuid.UUID(folder_id))
        result = await session.execute(stmt)
        messages = list(result.scalars().all())

    return [_message_summary(m) for m in messages]


@mcp.tool(
    name="get_mail",
    annotations={
        "title": "Get Mail",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_mail(mail_id: str) -> dict[str, Any]:
    """
    Get full email details by ID: subject, sender, recipients, body, flags.

    Args:
        mail_id: Message UUID

    Returns:
        Full message content, or {"error": "Message not found"}
    """
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Message).where(Message.id == uuid.UUID(mail_id)))
        msg = result.scalar_one_or_none()
    if msg is None:
        return {"error": "Message not found"}

    return {
        **_message_summary(msg),
        "message_id": msg.message_id,
        "to_addrs": msg.to_addrs,
        "cc_addrs": msg.cc_addrs,
        "body_text": msg.body_text,
        "is_answered": msg.is_answered,
        "is_draft": msg.is_draft,
        "keywords": msg.keywords or [],
    }


@mcp.tool(
    name="get_thread",
    annotations={
        "title": "Get Mail Thread",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_thread(mail_id: str) -> list[dict[str, Any]]:
    """
    Get every message in this message's conversation, across folders, ascending.

    Args:
        mail_id: Any message UUID belonging to the thread

    Returns:
        List of message summaries ordered oldest to newest, or a single
        {"error": ...} dict if the message does not exist
    """
    db = get_db_connection()
    async with db.session() as session:
        anchor = await session.execute(
            select(Message.thread_id).where(Message.id == uuid.UUID(mail_id))
        )
        thread_id = anchor.scalar_one_or_none()
        if thread_id is None:
            return [{"error": "Message not found"}]

        result = await session.execute(
            select(Message)
            .where(Message.thread_id == thread_id, Message.expunged_at.is_(None))
            .order_by(Message.received_at)
        )
        messages = list(result.scalars().all())

    return [_message_summary(m) for m in messages]


@mcp.tool(
    name="list_folders",
    annotations={
        "title": "List Folders",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def list_folders(account_id: str) -> list[dict[str, Any]]:
    """
    List all folders for an account.

    Args:
        account_id: Account UUID

    Returns:
        List of folders with id, imap_name, special_use, last_synced_at
    """
    db = get_db_connection()
    folder_repo = FolderRepository(db)
    folders = await folder_repo.get_by_account(uuid.UUID(account_id))
    return [
        {
            "id": str(f.id),
            "imap_name": f.imap_name,
            "display_name": f.display_name,
            "special_use": f.special_use,
            "last_synced_at": f.last_synced_at.isoformat() if f.last_synced_at else None,
        }
        for f in folders
    ]


@mcp.tool(
    name="list_accounts",
    annotations={
        "title": "List Accounts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def list_accounts() -> list[dict[str, Any]]:
    """
    List all configured mail accounts.

    Returns:
        List of accounts with id, name, host, active status, sync state
    """
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
            "state": a.state,
        }
        for a in accounts
    ]


@mcp.tool(
    name="move_mail",
    annotations={
        "title": "Move Mail",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def move_mail(mail_id: str, target_folder: str) -> dict[str, Any]:
    """
    Move a message to a different folder by that folder's IMAP name.

    PostIMAP's own trigger propagates the move to IMAP asynchronously.

    Args:
        mail_id: Message UUID to move
        target_folder: Target folder's IMAP name (see list_folders)

    Returns:
        {"success": bool, "message"/"error": str}
    """
    db = get_db_connection()
    async with db.session() as session:
        msg_result = await session.execute(
            select(Message).where(Message.id == uuid.UUID(mail_id))
        )
        msg = msg_result.scalar_one_or_none()
        if msg is None:
            return {"success": False, "error": "Message not found"}

        folder_result = await session.execute(
            select(Folder).where(
                Folder.account_id == msg.account_id, Folder.imap_name == target_folder,
            )
        )
        folder = folder_result.scalar_one_or_none()
        if folder is None:
            return {"success": False, "error": f"Folder not found: {target_folder}"}

        await move_message(session, msg.id, folder.id)

    return {"success": True, "message": f"Moved to {target_folder}"}


@mcp.tool(
    name="mark_mail",
    annotations={
        "title": "Mark Mail",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def mark_mail(
    mail_id: str,
    is_seen: bool | None = None,
    is_flagged: bool | None = None,
) -> dict[str, Any]:
    """
    Set read/unread and/or flagged/unflagged state on a message.

    Args:
        mail_id: Message UUID
        is_seen: Set read (true) or unread (false); omit to leave unchanged
        is_flagged: Set flagged (true) or unflagged (false); omit to leave unchanged

    Returns:
        {"success": bool, "error": str} on failure
    """
    flags: dict[str, bool] = {}
    if is_seen is not None:
        flags["is_seen"] = is_seen
    if is_flagged is not None:
        flags["is_flagged"] = is_flagged
    if not flags:
        return {"success": False, "error": "Provide at least one of is_seen, is_flagged"}

    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Message.id).where(Message.id == uuid.UUID(mail_id))
        )
        if result.scalar_one_or_none() is None:
            return {"success": False, "error": "Message not found"}
        await set_flags(session, uuid.UUID(mail_id), **flags)

    return {"success": True}


@mcp.tool(
    name="tag_mail",
    annotations={
        "title": "Tag Mail",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def tag_mail(mail_id: str, tag_name: str, source: str = "user") -> dict[str, Any]:
    """
    Add a tag to a message.

    Args:
        mail_id: Message UUID
        tag_name: Tag string to add
        source: Tag source: user, rule, enrichment, spam, or imap (default user)

    Returns:
        {"success": bool, "tag_name": str, "source": str}
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


@mcp.tool(
    name="get_verdict",
    annotations={
        "title": "Get Spam Verdict",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_verdict(mail_id: str) -> dict[str, Any] | None:
    """
    Get the latest spam verdict for a message.

    Args:
        mail_id: Message UUID

    Returns:
        Verdict details (is_spam, model_used, reasoning, source, created_at)
        or None if no verdict has been issued
    """
    db = get_db_connection()
    verdict_repo = VerdictRepository(db)
    verdict = await verdict_repo.get_latest_for_mail(uuid.UUID(mail_id))
    if verdict is None:
        return None

    return {
        "id": str(verdict.id),
        "message_id": str(verdict.mail_id),
        "is_spam": verdict.is_spam,
        "model_used": verdict.model_used,
        "reasoning": verdict.reasoning,
        "source": verdict.source.value,
        "created_at": verdict.created_at.isoformat(),
    }


@mcp.tool(
    name="submit_spam_feedback",
    annotations={
        "title": "Submit Spam Feedback",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def submit_spam_feedback(mail_id: str, account_id: str, is_spam: bool) -> dict[str, Any]:
    """
    Correct a message's spam classification, recording a user_feedback verdict.

    Does not move the message -- pair with move_mail if the correction
    should also relocate it (e.g. out of Junk back to the inbox).

    Args:
        mail_id: Message UUID
        account_id: Account UUID the message belongs to
        is_spam: The corrected classification

    Returns:
        {"success": bool, "message": str}
    """
    from mail_verdict.server import get_spam_processor

    processor = get_spam_processor()
    if processor is None:
        return {"success": False, "error": "Spam feedback handler not available"}

    feedback = processor.feedback
    ok = (
        await feedback.handle_moved_to_spam(uuid.UUID(mail_id), uuid.UUID(account_id))
        if is_spam
        else await feedback.handle_moved_from_spam(uuid.UUID(mail_id), uuid.UUID(account_id))
    )
    return {"success": ok, "message": "Feedback recorded" if ok else "Feedback processing failed"}


async def _create_outbox_row(
    account_id: str,
    kind: str,
    to: list[str],
    subject: str,
    body_text: str,
    cc: list[str] | None,
    bcc: list[str] | None,
    in_reply_to: str | None,
    references: list[str] | None,
) -> dict[str, Any]:
    """Shared insert path for send_mail and draft_mail."""
    db = get_db_connection()
    async with db.session() as session:
        outbox = await insert_outbox(
            session,
            account_id=uuid.UUID(account_id),
            kind=kind,
            to_addrs=to,
            cc_addrs=cc,
            bcc_addrs=bcc,
            subject=subject,
            body_text=body_text,
            in_reply_to=in_reply_to,
            references=references,
        )
        return {"success": True, "outbox_id": str(outbox.id), "status": outbox.status}


@mcp.tool(
    name="send_mail",
    annotations={
        "title": "Send Mail",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def send_mail(
    account_id: str,
    to: list[str],
    subject: str,
    body_text: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> dict[str, Any]:
    """
    Send an email through the given account's SMTP settings.

    Inserts an outbox row; PostIMAP composes and sends it, then appends a
    copy to Sent. This is irreversible once accepted by the SMTP server --
    there is no undo-send.

    Args:
        account_id: Account UUID to send from (must have smtp_host/smtp_port set)
        to: Recipient email addresses
        subject: Message subject
        body_text: Plain text body
        cc: CC addresses, optional
        bcc: BCC addresses, optional
        in_reply_to: Message-ID header of the message being replied to, with
            angle brackets, for threading (optional)
        references: Full References chain for threading (optional)

    Returns:
        {"success": bool, "outbox_id": str, "status": str} -- status starts
        "pending"; poll get_stats or list_mails, or watch outbox.updated SSE,
        to see it transition to sent/failed/dead
    """
    return await _create_outbox_row(
        account_id, "send", to, subject, body_text, cc, bcc, in_reply_to, references,
    )


@mcp.tool(
    name="draft_mail",
    annotations={
        "title": "Draft Mail",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def draft_mail(
    account_id: str,
    to: list[str],
    subject: str,
    body_text: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> dict[str, Any]:
    """
    Save a draft to the account's Drafts folder without sending it.

    Args:
        account_id: Account UUID
        to: Recipient email addresses
        subject: Message subject
        body_text: Plain text body
        cc: CC addresses, optional
        bcc: BCC addresses, optional
        in_reply_to: Message-ID header of the message being replied to, optional
        references: Full References chain for threading, optional

    Returns:
        {"success": bool, "outbox_id": str, "status": str}
    """
    return await _create_outbox_row(
        account_id, "draft", to, subject, body_text, cc, bcc, in_reply_to, references,
    )


@mcp.tool(
    name="get_stats",
    annotations={
        "title": "Get Spam Detection Stats",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_stats(account_id: str | None = None) -> dict[str, Any]:
    """
    Get spam detection statistics.

    Args:
        account_id: Optional account UUID to scope stats to one account

    Returns:
        Totals and accuracy across verdicts (total_verdicts, spam_count,
        ham_count, false_positives, false_negatives, accuracy, fp_rate, fn_rate)
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

    async with db.session() as session:
        accts = await session.execute(select(Account.id))
        account_ids = [row[0] for row in accts.all()]

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


@mcp.tool(
    name="semantic_search_mail",
    annotations={
        "title": "Semantic Search Mail",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def semantic_search_mail(
    query: str,
    account_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Find mail by meaning rather than exact words -- "that email about the
    conference I said I might attend" finds it even with none of those
    words in the message. Complements search_mail: use search_mail for a
    known sender or an exact phrase, this for a half-remembered topic.

    Args:
        query: What to search for, in plain language
        account_id: Optional account UUID to scope the search to one account
        limit: Max results, 1-100 (default 20)

    Returns:
        List of message summaries with a similarity score (0-1, higher is
        closer), ranked nearest first. Only messages already encoded with
        the currently configured model are searched -- see get_semantic_status
        for coverage.
    """
    from mail_verdict.embeddings.provider import (
        DEFAULT_EMBEDDING_MODEL,
        resolve_embedding_provider,
    )
    from mail_verdict.embeddings.search import semantic_search
    from mail_verdict.settings.credentials import get_provider_credential_repo
    from mail_verdict.settings.service import get_settings_service

    settings = get_settings_service().get("semantic")
    model = str(settings.get("model", DEFAULT_EMBEDDING_MODEL))
    provider = resolve_embedding_provider(
        str(settings.get("provider", "openai")), get_provider_credential_repo(),
    )
    vectors = await provider.embed_batch([query], model=model)

    aid = uuid.UUID(account_id) if account_id else None
    hits = await semantic_search(
        get_db_connection(), query_vector=vectors[0], model=model, account_id=aid, k=limit,
    )
    return [{**_message_summary(hit.message), "similarity": hit.similarity} for hit in hits]


@mcp.tool(
    name="get_semantic_status",
    annotations={
        "title": "Get Semantic Search Coverage",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_semantic_status(account_id: str | None = None) -> dict[str, Any]:
    """
    Coverage of the semantic search index -- how much of the mailbox has a
    vector under the currently configured model.

    Args:
        account_id: Optional account UUID to scope to one account

    Returns:
        model, in_scope, encoded, pending, failed, coverage (0-1)
    """
    from mail_verdict.embeddings.provider import DEFAULT_EMBEDDING_MODEL
    from mail_verdict.embeddings.repository import EmbeddingRepository
    from mail_verdict.settings.service import get_settings_service

    settings = get_settings_service().get("semantic")
    model = str(settings.get("model", DEFAULT_EMBEDDING_MODEL))
    aid = uuid.UUID(account_id) if account_id else None
    status = await EmbeddingRepository(get_db_connection()).status(model=model, account_id=aid)
    return {
        "model": status.model, "in_scope": status.in_scope, "encoded": status.encoded,
        "pending": status.pending, "failed": status.failed, "coverage": status.coverage,
    }
