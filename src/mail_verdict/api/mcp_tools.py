"""
MCP server tools for MailVerdict.

Reads from Postgres, writes through postimap/actions.py -- never touches
IMAP/SMTP directly. Mail tools: search_mail, list_mails, get_mail,
get_thread, list_folders, list_accounts, move_mail, mark_mail, tag_mail,
get_verdict, submit_spam_feedback, send_mail, draft_mail, get_stats,
semantic_search_mail, get_semantic_status. Calendar and contact tools:
list_calendars, list_events, get_event, create_event, update_event,
delete_event, respond_to_event, list_addressbooks, list_contacts,
search_contacts, get_contact, create_contact, update_contact,
delete_contact.

The calendar and contact tools wrap the same api/calendar_events.py,
api/calendars.py and api/contacts.py functions the REST endpoints call --
never a second copy of that logic. Each is called directly as a plain
async function (the @router decorator does not wrap it), with its
HTTPException translated into the {"error": ...} shape these tools use
instead of raising.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from fastmcp import FastMCP
from sqlalchemy import desc, select

from mail_verdict.api.calendar_events import (
    create_event as _create_calendar_event,
)
from mail_verdict.api.calendar_events import (
    delete_event as _delete_calendar_event,
)
from mail_verdict.api.calendar_events import (
    get_event as _get_calendar_event,
)
from mail_verdict.api.calendar_events import (
    list_events as _list_calendar_events,
)
from mail_verdict.api.calendar_events import (
    respond_to_event as _respond_to_calendar_event,
)
from mail_verdict.api.calendar_events import (
    update_event as _update_calendar_event,
)
from mail_verdict.api.calendars import (
    list_addressbooks as _list_addressbooks,
)
from mail_verdict.api.calendars import (
    list_calendars as _list_calendars,
)
from mail_verdict.api.contacts import (
    create_contact as _create_contact,
)
from mail_verdict.api.contacts import (
    delete_contact as _delete_contact,
)
from mail_verdict.api.contacts import (
    get_contact as _get_contact,
)
from mail_verdict.api.contacts import (
    list_contacts as _list_contacts,
)
from mail_verdict.api.contacts import (
    search_contacts as _search_contacts,
)
from mail_verdict.api.contacts import (
    update_contact as _update_contact,
)
from mail_verdict.api.identities import resolve_send_from_addr
from mail_verdict.api.schemas import (
    ContactAddressIO,
    ContactCreateRequest,
    ContactEmailIO,
    ContactPhoneIO,
    ContactUpdateRequest,
    EventAttendeeIn,
    EventCreateRequest,
    EventDeleteRequest,
    EventUpdateRequest,
    RespondRequest,
)
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
    identity_id: str | None,
) -> dict[str, Any]:
    """Shared insert path for send_mail and draft_mail."""
    db = get_db_connection()
    account_uuid = uuid.UUID(account_id)
    async with db.session() as session:
        from_addr = await resolve_send_from_addr(
            session, account_uuid, uuid.UUID(identity_id) if identity_id else None,
        )
        outbox = await insert_outbox(
            session,
            account_id=account_uuid,
            kind=kind,
            from_addr=from_addr,
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
    identity_id: str | None = None,
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
        identity_id: Identity UUID to send as, optional -- falls back to the
            account's default identity, or its imap_user if it has none.
            Identities are managed via the REST API's /identities endpoints,
            not exposed as an MCP tool

    Returns:
        {"success": bool, "outbox_id": str, "status": str} -- status starts
        "pending"; poll get_stats or list_mails, or watch outbox.updated SSE,
        to see it transition to sent/failed/dead
    """
    return await _create_outbox_row(
        account_id, "send", to, subject, body_text, cc, bcc, in_reply_to, references, identity_id,
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
    identity_id: str | None = None,
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
        identity_id: Identity UUID to draft as, optional -- falls back to the
            account's default identity, or its imap_user if it has none

    Returns:
        {"success": bool, "outbox_id": str, "status": str}
    """
    return await _create_outbox_row(
        account_id, "draft", to, subject, body_text, cc, bcc, in_reply_to, references, identity_id,
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
        closer), ranked nearest first -- up to limit, or fewer if the
        rest don't clear the account's configured strictness cutoff (see
        embeddings/search.py: relative to the best match in the pool, not
        an absolute floor). Only messages already encoded with the
        currently configured model are searched -- see get_semantic_status
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
    strictness = settings.get("default_strictness", "balanced")
    provider = resolve_embedding_provider(
        str(settings.get("provider", "openai")), get_provider_credential_repo(),
    )
    vectors = await provider.embed_batch([query], model=model)

    aid = uuid.UUID(account_id) if account_id else None
    outcome = await semantic_search(
        get_db_connection(), query_vector=vectors[0], model=model, account_id=aid,
        k=limit, strictness=strictness,
    )
    return [
        {**_message_summary(hit.message), "similarity": hit.similarity}
        for hit in outcome.results
    ]


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


def _endpoint_error(exc: HTTPException) -> dict[str, Any]:
    """An api/ router's HTTPException, in the {"error": ...} shape these
    tools return instead of letting it surface as a raised exception."""
    return {"error": str(exc.detail)}


@mcp.tool(
    name="list_calendars",
    annotations={
        "title": "List Calendars",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def list_calendars() -> list[dict[str, Any]] | dict[str, Any]:
    """
    List every calendar across every DAV account.

    Returns:
        List of calendars with id, display_name, dav_account_name, color,
        is_visible, read_only, identity_id, intake, supported_components,
        total_count -- or {"error": ...} if calendars are unsupported
    """
    try:
        calendars = await _list_calendars()
    except HTTPException as exc:
        return _endpoint_error(exc)
    return [c.model_dump(mode="json") for c in calendars]


@mcp.tool(
    name="list_events",
    annotations={
        "title": "List Calendar Events",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def list_events(
    month: str, calendar_ids: str | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """
    List every event instance in one calendar-month window, recurring
    series expanded, across every visible calendar unless calendar_ids
    narrows it.

    Args:
        month: The window to list, "YYYY-MM"
        calendar_ids: Optional comma-separated calendar UUIDs to scope to

    Returns:
        List of event instances (see get_event for the shape), or
        {"error": ...} on an invalid month or an unsupported server
    """
    try:
        result = await _list_calendar_events(month, calendar_ids)
    except HTTPException as exc:
        return _endpoint_error(exc)
    return [e.model_dump(mode="json") for e in result.events]


@mcp.tool(
    name="get_event",
    annotations={
        "title": "Get Calendar Event",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_event(event_id: str, recurrence_id: str | None = None) -> dict[str, Any]:
    """
    Get one event instance -- the master, or one named occurrence of a
    recurring series.

    Args:
        event_id: Event UUID (see list_events)
        recurrence_id: Occurrence to fetch, or omit for the master

    Returns:
        summary, dtstart, dtend, all_day, location, description, status,
        sequence, organizer, attendees, own partstat, rrule expressed as
        is_recurring/is_exception, and read_only -- or {"error": ...}
    """
    try:
        instance = await _get_calendar_event(uuid.UUID(event_id), recurrence_id)
    except HTTPException as exc:
        return _endpoint_error(exc)
    return instance.model_dump(mode="json")


@mcp.tool(
    name="create_event",
    annotations={
        "title": "Create Calendar Event",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def create_event(
    calendar_id: str,
    summary: str,
    dtstart: str,
    dtend: str,
    all_day: bool = False,
    location: str | None = None,
    description: str | None = None,
    rrule: str | None = None,
    tz: str | None = None,
    attendee_emails: list[str] | None = None,
) -> dict[str, Any]:
    """
    Create an event. With attendees, the calendar needs a linked identity
    -- MailVerdict sends the invitations itself over its outbox rather
    than the server's own scheduling engine.

    Args:
        calendar_id: Calendar UUID to create the event in (see list_calendars)
        summary: Event title
        dtstart: Start time, ISO 8601 (a bare date if all_day)
        dtend: End time, ISO 8601, exclusive if all_day
        all_day: Whether dtstart/dtend are dates rather than datetimes
        location: Location text, optional
        description: Description text, optional
        rrule: A raw RRULE value (e.g. "FREQ=WEEKLY;INTERVAL=2;COUNT=6" or
            "FREQ=DAILY;UNTIL=20261231T000000Z") -- the full RFC 5545
            vocabulary, not a fixed preset
        tz: An IANA zone name (e.g. "Europe/Berlin") to bind dtstart/dtend
            to, so the stored event carries a named zone rather than only
            a fixed UTC offset -- correct across a DST change a fixed
            offset is not. dtstart/dtend's own wall-clock reading is kept;
            only the zone they resolve against changes. Not valid with
            all_day, which has no time-of-day to bind
        attendee_emails: Email addresses to invite, optional

    Returns:
        The created event instance, or {"error": ...} on failure -- e.g.
        attendees given but the calendar has no linked identity, or an
        unrecognised tz
    """
    request = EventCreateRequest(
        calendar_id=calendar_id,  # type: ignore[arg-type]
        summary=summary, dtstart=dtstart, dtend=dtend,  # type: ignore[arg-type]
        all_day=all_day, location=location, description=description, rrule=rrule, tz=tz,
        attendees=(
            [EventAttendeeIn(email=e) for e in attendee_emails] if attendee_emails else None
        ),
    )
    try:
        instance = await _create_calendar_event(request)
    except HTTPException as exc:
        return _endpoint_error(exc)
    return instance.model_dump(mode="json")


@mcp.tool(
    name="update_event",
    annotations={
        "title": "Update Calendar Event",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def update_event(
    event_id: str,
    calendar_id: str | None = None,
    summary: str | None = None,
    dtstart: str | None = None,
    dtend: str | None = None,
    all_day: bool | None = None,
    location: str | None = None,
    description: str | None = None,
    rrule: str | None = None,
    scope: str = "all",
    recurrence_id: str | None = None,
) -> dict[str, Any]:
    """
    Edit an event. Fields left unset are unchanged. scope="all" edits the
    whole series (or the only edit path for a non-recurring event);
    scope="this" edits one occurrence without touching the others and
    needs recurrence_id; scope="following" is not implemented and is
    refused. An event this identity organizes with attendees sends an
    updated invitation.

    Args:
        event_id: Event UUID to edit
        calendar_id: Move the event to a different calendar, optional
        summary: New title, optional
        dtstart: New start time, ISO 8601, optional
        dtend: New end time, ISO 8601, optional
        all_day: New all_day flag, optional
        location: New location text, optional
        description: New description text, optional
        rrule: New raw RRULE value, or "" to remove recurrence -- scope="all" only
        scope: "this" or "all" (default)
        recurrence_id: Required with scope="this"

    Returns:
        The updated event instance, or {"error": ...} on failure
    """
    request = EventUpdateRequest(
        calendar_id=calendar_id,  # type: ignore[arg-type]
        summary=summary, dtstart=dtstart, dtend=dtend,  # type: ignore[arg-type]
        all_day=all_day, location=location, description=description, rrule=rrule,
        scope=scope,  # type: ignore[arg-type]
        recurrence_id=recurrence_id,
    )
    try:
        instance = await _update_calendar_event(uuid.UUID(event_id), request)
    except HTTPException as exc:
        return _endpoint_error(exc)
    return instance.model_dump(mode="json")


@mcp.tool(
    name="delete_event",
    annotations={
        "title": "Delete Calendar Event",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def delete_event(
    event_id: str, scope: str | None = None, recurrence_id: str | None = None,
) -> dict[str, Any]:
    """
    Delete an event. scope="all" (default, or the only path for a
    non-recurring event) removes it from the calendar; scope="this"
    cancels that one occurrence instead, since the series still exists
    for every other occurrence. An event this identity organizes with
    attendees sends a cancellation first.

    Args:
        event_id: Event UUID to delete
        scope: "this" or "all" (default); "following" is refused
        recurrence_id: Required with scope="this"

    Returns:
        {"success": bool, "error": str} on failure
    """
    request = (
        EventDeleteRequest(scope=scope, recurrence_id=recurrence_id)  # type: ignore[arg-type]
        if scope is not None or recurrence_id is not None
        else None
    )
    try:
        await _delete_calendar_event(uuid.UUID(event_id), request)
    except HTTPException as exc:
        return {"success": False, **_endpoint_error(exc)}
    return {"success": True}


@mcp.tool(
    name="respond_to_event",
    annotations={
        "title": "Respond To Calendar Invitation",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def respond_to_event(
    event_id: str,
    identity_id: str,
    partstat: str,
    comment: str | None = None,
    recurrence_id: str | None = None,
) -> dict[str, Any]:
    """
    Accept, decline or tentatively accept an invitation: updates this
    identity's own attendance status immediately, and sends the reply
    over its outbox.

    Args:
        event_id: Event UUID
        identity_id: Identity UUID responding (must be an attendee)
        partstat: "accepted", "declined" or "tentative"
        comment: Optional comment included in the reply
        recurrence_id: Required to respond to one occurrence of a series

    Returns:
        The updated event instance, or {"error": ...} on failure
    """
    request = RespondRequest(
        identity_id=identity_id,  # type: ignore[arg-type]
        partstat=partstat,  # type: ignore[arg-type]
        comment=comment, recurrence_id=recurrence_id,
    )
    try:
        instance = await _respond_to_calendar_event(uuid.UUID(event_id), request)
    except HTTPException as exc:
        return _endpoint_error(exc)
    return instance.model_dump(mode="json")


@mcp.tool(
    name="list_addressbooks",
    annotations={
        "title": "List Address Books",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def list_addressbooks() -> list[dict[str, Any]] | dict[str, Any]:
    """
    List every address book across every DAV account.

    Returns:
        List of address books with id, display_name, dav_account_name,
        read_only, total_count -- or {"error": ...} if unsupported
    """
    try:
        addressbooks = await _list_addressbooks()
    except HTTPException as exc:
        return _endpoint_error(exc)
    return [a.model_dump(mode="json") for a in addressbooks]


@mcp.tool(
    name="list_contacts",
    annotations={
        "title": "List Contacts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def list_contacts(
    addressbook_id: str | None = None, q: str | None = None, limit: int = 50,
) -> list[dict[str, Any]] | dict[str, Any]:
    """
    List contacts, optionally scoped to one address book or filtered by a
    text query. Always paged -- an address book can hold thousands of rows.

    Args:
        addressbook_id: Optional address book UUID to scope to
        q: Optional text filter over name and email
        limit: Max results, 1-200 (default 50)

    Returns:
        List of contacts (see get_contact for the shape), or
        {"error": ...} if unsupported
    """
    try:
        result = await _list_contacts(
            uuid.UUID(addressbook_id) if addressbook_id else None, q, min(limit, 200), None,
        )
    except HTTPException as exc:
        return _endpoint_error(exc)
    return [c.model_dump(mode="json") for c in result.contacts]


@mcp.tool(
    name="search_contacts",
    annotations={
        "title": "Search Contacts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def search_contacts(q: str) -> list[dict[str, Any]] | dict[str, Any]:
    """
    Find contacts by name or email -- one row per email address, matching
    what the compose autocomplete searches.

    Args:
        q: Search text, at least one character

    Returns:
        List of {contact_id, name, email, source}, or {"error": ...}
    """
    try:
        hits = await _search_contacts(q)
    except HTTPException as exc:
        return _endpoint_error(exc)
    return [h.model_dump(mode="json") for h in hits]


@mcp.tool(
    name="get_contact",
    annotations={
        "title": "Get Contact",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_contact(contact_id: str) -> dict[str, Any]:
    """
    Get full contact detail, parsed from the stored vCard.

    Args:
        contact_id: Contact UUID

    Returns:
        summary, emails, organization, title, phones, addresses,
        birthday, urls, notes, categories, photo -- or {"error": ...}
        if not found
    """
    try:
        contact = await _get_contact(uuid.UUID(contact_id))
    except HTTPException as exc:
        return _endpoint_error(exc)
    return contact.model_dump(mode="json")


def _contact_emails(emails: list[dict[str, Any]] | None) -> list[ContactEmailIO]:
    return [ContactEmailIO(email=e["email"], type=e.get("type")) for e in emails or []]


def _contact_phones(phones: list[dict[str, Any]] | None) -> list[ContactPhoneIO]:
    return [ContactPhoneIO(number=p["number"], type=p.get("type")) for p in phones or []]


def _contact_addresses(addresses: list[dict[str, Any]] | None) -> list[ContactAddressIO]:
    return [ContactAddressIO(label=a.get("label"), text=a["text"]) for a in addresses or []]


@mcp.tool(
    name="create_contact",
    annotations={
        "title": "Create Contact",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def create_contact(
    addressbook_id: str,
    summary: str,
    emails: list[dict[str, Any]] | None = None,
    organization: str | None = None,
    title: str | None = None,
    phones: list[dict[str, Any]] | None = None,
    addresses: list[dict[str, Any]] | None = None,
    birthday: str | None = None,
    urls: list[str] | None = None,
    notes: str | None = None,
    categories: list[str] | None = None,
) -> dict[str, Any]:
    """
    Create a contact in an address book.

    Args:
        addressbook_id: Address book UUID (see list_addressbooks)
        summary: Display name
        emails: [{"email": str, "type": str | None}, ...], optional
        organization: Organization name, optional
        title: Job title, optional
        phones: [{"number": str, "type": str | None}, ...], optional
        addresses: [{"label": str | None, "text": str}, ...], optional
        birthday: Birthday, optional
        urls: Websites, optional
        notes: Free text notes, optional
        categories: Tags, optional

    Returns:
        The created contact, or {"error": ...} on failure
    """
    request = ContactCreateRequest(
        addressbook_id=addressbook_id,  # type: ignore[arg-type]
        summary=summary, emails=_contact_emails(emails), organization=organization, title=title,
        phones=_contact_phones(phones), addresses=_contact_addresses(addresses),
        birthday=birthday, urls=urls or [], notes=notes, categories=categories or [],
    )
    try:
        contact = await _create_contact(request)
    except HTTPException as exc:
        return _endpoint_error(exc)
    return contact.model_dump(mode="json")


@mcp.tool(
    name="update_contact",
    annotations={
        "title": "Update Contact",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def update_contact(
    contact_id: str,
    summary: str | None = None,
    emails: list[dict[str, Any]] | None = None,
    organization: str | None = None,
    title: str | None = None,
    phones: list[dict[str, Any]] | None = None,
    addresses: list[dict[str, Any]] | None = None,
    birthday: str | None = None,
    urls: list[str] | None = None,
    notes: str | None = None,
    categories: list[str] | None = None,
) -> dict[str, Any]:
    """
    Edit a contact. Every field given is a full replacement of that
    property -- e.g. giving emails replaces the whole email list, it does
    not append to it. Fields left unset are unchanged.

    Args:
        contact_id: Contact UUID
        summary: New display name, optional
        emails: New [{"email": str, "type": str | None}, ...], optional
        organization: New organization name, optional
        title: New job title, optional
        phones: New [{"number": str, "type": str | None}, ...], optional
        addresses: New [{"label": str | None, "text": str}, ...], optional
        birthday: New birthday, optional
        urls: New websites, optional
        notes: New free text notes, optional
        categories: New tags, optional

    Returns:
        The updated contact, or {"error": ...} on failure
    """
    # update_contact's own endpoint reads request.model_dump(exclude_unset=True)
    # -- a field passed explicitly as None here would still count as "set"
    # and be read back as "clear this field", not "leave it unchanged". Only
    # fields actually given are included, exactly as mark_mail does above.
    fields: dict[str, Any] = {}
    if summary is not None:
        fields["summary"] = summary
    if emails is not None:
        fields["emails"] = _contact_emails(emails)
    if organization is not None:
        fields["organization"] = organization
    if title is not None:
        fields["title"] = title
    if phones is not None:
        fields["phones"] = _contact_phones(phones)
    if addresses is not None:
        fields["addresses"] = _contact_addresses(addresses)
    if birthday is not None:
        fields["birthday"] = birthday
    if urls is not None:
        fields["urls"] = urls
    if notes is not None:
        fields["notes"] = notes
    if categories is not None:
        fields["categories"] = categories
    request = ContactUpdateRequest(**fields)
    try:
        contact = await _update_contact(uuid.UUID(contact_id), request)
    except HTTPException as exc:
        return _endpoint_error(exc)
    return contact.model_dump(mode="json")


@mcp.tool(
    name="delete_contact",
    annotations={
        "title": "Delete Contact",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def delete_contact(contact_id: str) -> dict[str, Any]:
    """
    Delete a contact.

    Args:
        contact_id: Contact UUID

    Returns:
        {"success": bool, "error": str} on failure
    """
    try:
        await _delete_contact(uuid.UUID(contact_id))
    except HTTPException as exc:
        return {"success": False, **_endpoint_error(exc)}
    return {"success": True}
