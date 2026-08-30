"""
MessageView: the immutable, narrow snapshot a stage actually sees.

Loaded fresh at run execution time, never carried on the queue row (see
pipeline/runner.py) -- the queue row only ever holds (account_id, msg_key).

The loader selects a fixed, short column list. It never selects
raw_source (the full RFC822 bytea) and never selects a whole Attachment
row -- rules/engine.py's context builder did both, which at pipeline
concurrency is an out-of-memory pod restart in the middle of a backfill
that looks like anything but its actual cause.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr

import nh3
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import Attachment, Folder, FolderPrefs, MailTag, Message

# How much of the body a stage ever sees. Long enough for a model to judge
# tone and intent, short enough that a 20MB message costs nothing to load.
_BODY_EXCERPT_CHARS = 4_000


@dataclass(frozen=True)
class FolderView:
    """The folder a message currently sits in, as seen at execution time."""

    id: uuid.UUID
    imap_name: str
    special_use: str | None


@dataclass(frozen=True)
class MessageView:
    """An immutable snapshot of one message, as of the moment a run executed."""

    message_id: uuid.UUID
    msg_key: str
    account_id: uuid.UUID
    folder: FolderView
    subject: str
    from_addr: str
    to_addrs: tuple[str, ...]
    cc_addrs: tuple[str, ...]
    headers: dict[str, str]
    body: str
    body_truncated: bool
    size_bytes: int
    received_at: datetime | None
    is_seen: bool
    is_flagged: bool
    is_draft: bool
    is_truncated: bool
    keywords: tuple[str, ...]
    tags: tuple[str, ...]
    attachment_types: tuple[str, ...]
    has_attachments: bool
    reply_to: str | None = None

    def with_folder(self, folder: FolderView) -> MessageView:
        """A copy with a different folder -- how the runner projects an
        applied Move effect so a later stage in the same run sees the
        destination rather than stale, pre-move state."""
        return _replace(self, folder=folder)

    def with_flags(self, **flags: bool) -> MessageView:
        """A copy with one or more flags overridden -- the SetFlags projection."""
        return _replace(self, **flags)

    def with_keywords(self, keywords: tuple[str, ...]) -> MessageView:
        """A copy with a different keyword tuple -- the Keywords projection."""
        return _replace(self, keywords=keywords)

    def with_tags(self, tags: tuple[str, ...]) -> MessageView:
        """A copy with a different tag tuple -- the Tag projection."""
        return _replace(self, tags=tags)


def _replace(view: MessageView, **changes: object) -> MessageView:
    from dataclasses import replace

    return replace(view, **changes)  # type: ignore[arg-type]


def _addr_list(value: object) -> tuple[str, ...]:
    """Normalize a jsonb address column (list of strings or dicts) to a
    flat tuple of address strings."""
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and "address" in item:
                out.append(str(item["address"]))
        return tuple(out)
    return ()


def _strip_html(html: str) -> str:
    """Reduce HTML to its text content -- nh3 with no allowed tags keeps
    every tag's inner text while discarding the markup itself."""
    return nh3.clean(html, tags=set()).strip()


def extract_display_name_and_addr(from_header: str) -> tuple[str, str]:
    """Split a From header into its display name and bare address."""
    display_name, addr = parseaddr(from_header or "")
    return display_name, addr


def build_identity_facts(view: MessageView) -> dict[str, object]:
    """
    Facts about the relationship between the From header and everything
    around it -- what the classify stage hands the model in place of
    trusting From on its own.

    From is free text anyone can write; what is comparatively harder to
    forge is whether it agrees with the envelope sender, the Return-Path,
    Reply-To, and the address embedded in its own display name. Each
    signal is stated as a fact for the model to weigh, never pre-judged
    into a score here.
    """
    display_name, from_addr = extract_display_name_and_addr(view.from_addr)
    return_path = _header(view.headers, "return-path")
    return_path_addr = parseaddr(return_path)[1] if return_path else None
    reply_to = view.reply_to or _header(view.headers, "reply-to")
    reply_to_addr = parseaddr(reply_to)[1] if reply_to else None

    display_name_addr = None
    if "@" in display_name:
        display_name_addr = parseaddr(display_name)[1] or None

    return {
        "from_display_name": display_name or None,
        "from_addr": from_addr or None,
        "return_path_addr": return_path_addr,
        "return_path_matches_from": (
            return_path_addr.lower() == from_addr.lower()
            if return_path_addr and from_addr
            else None
        ),
        "reply_to_addr": reply_to_addr,
        "reply_to_matches_from": (
            reply_to_addr.lower() == from_addr.lower() if reply_to_addr and from_addr else None
        ),
        "display_name_contains_different_address": (
            display_name_addr is not None and display_name_addr.lower() != from_addr.lower()
        ),
        "dkim": _auth_result(view.headers, "dkim"),
        "spf": _auth_result(view.headers, "spf"),
        "dmarc": _auth_result(view.headers, "dmarc"),
    }


def _header(headers: dict[str, str], name: str) -> str | None:
    return headers.get(name) or headers.get(name.lower())


def _auth_result(headers: dict[str, str], protocol: str) -> str:
    """pass / fail / unknown for one protocol out of Authentication-Results."""
    auth_results = _header(headers, "authentication-results") or ""
    auth_str = auth_results.lower()
    if f"{protocol}=pass" in auth_str:
        return "pass"
    if f"{protocol}=fail" in auth_str or f"{protocol}=softfail" in auth_str:
        return "fail"
    return "unknown"


async def load_message_view(session: AsyncSession, message_id: uuid.UUID) -> MessageView | None:
    """
    Load a MessageView by current messages.id.

    Returns None if the message no longer exists or its folder is gone --
    the runner treats that as "skipped: message gone", the same outcome a
    guarded effect produces when the message vanishes mid-run.
    """
    from mail_verdict.database.msg_key import compute_msg_key

    result = await session.execute(
        select(
            Message.id,
            Message.account_id,
            Message.folder_id,
            Message.message_id,
            Message.subject,
            Message.from_addr,
            Message.to_addrs,
            Message.cc_addrs,
            Message.reply_to,
            Message.raw_headers,
            Message.body_text,
            Message.body_html,
            Message.size_bytes,
            Message.received_at,
            Message.is_seen,
            Message.is_flagged,
            Message.is_draft,
            Message.is_truncated,
            Message.keywords,
            Message.expunged_at,
        ).where(Message.id == message_id)
    )
    row = result.one_or_none()
    if row is None or row.expunged_at is not None:
        return None

    # folder_prefs.special_use_override exists for servers that don't
    # advertise SPECIAL-USE. Reading Folder.special_use raw here would
    # disagree with the enqueue-time gate (pipeline/enqueue.py), which
    # coalesces the override in -- a message the gate let through as
    # in-scope would then look out-of-scope to the runner's own re-check.
    folder_result = await session.execute(
        select(
            Folder.id,
            Folder.imap_name,
            func.coalesce(FolderPrefs.special_use_override, Folder.special_use).label(
                "special_use"
            ),
            Folder.deleted_at,
        )
        .outerjoin(FolderPrefs, Folder.id == FolderPrefs.folder_id)
        .where(Folder.id == row.folder_id)
    )
    folder_row = folder_result.one_or_none()
    if folder_row is None or folder_row.deleted_at is not None:
        return None

    # Only content_type: never the attachment blob itself. Selecting whole
    # Attachment rows (including `data`) to answer "are there any, and what
    # kind" is the OOM-under-concurrency bug this loader exists to avoid.
    att_result = await session.execute(
        select(Attachment.content_type).where(Attachment.message_id == row.id)
    )
    att_rows = att_result.all()
    attachment_types = tuple(ct for (ct,) in att_rows if ct)
    has_attachments = bool(att_rows)

    tag_result = await session.execute(select(MailTag.tag_name).where(MailTag.mail_id == row.id))
    tags = tuple(name for (name,) in tag_result.all())

    if row.body_text:
        body = row.body_text[:_BODY_EXCERPT_CHARS]
        truncated = len(row.body_text) > _BODY_EXCERPT_CHARS
    elif row.body_html:
        stripped = _strip_html(row.body_html)
        body, truncated = stripped[:_BODY_EXCERPT_CHARS], len(stripped) > _BODY_EXCERPT_CHARS
    else:
        body, truncated = "", False

    headers = row.raw_headers if isinstance(row.raw_headers, dict) else {}
    headers = {str(k).lower(): str(v) for k, v in headers.items()}

    msg_key = compute_msg_key(
        account_id=row.account_id,
        message_id_hdr=row.message_id,
        from_addr=row.from_addr,
        subject=row.subject,
        received_at=row.received_at,
        size_bytes=row.size_bytes,
    )

    return MessageView(
        message_id=row.id,
        msg_key=msg_key,
        account_id=row.account_id,
        folder=FolderView(
            id=folder_row.id, imap_name=folder_row.imap_name, special_use=folder_row.special_use,
        ),
        subject=row.subject or "",
        from_addr=row.from_addr or "",
        to_addrs=_addr_list(row.to_addrs),
        cc_addrs=_addr_list(row.cc_addrs),
        headers=headers,
        body=body,
        body_truncated=truncated,
        size_bytes=row.size_bytes or 0,
        received_at=row.received_at,
        is_seen=row.is_seen,
        is_flagged=row.is_flagged,
        is_draft=row.is_draft,
        is_truncated=row.is_truncated,
        keywords=tuple(row.keywords or ()),
        tags=tags,
        attachment_types=attachment_types,
        has_attachments=has_attachments,
        reply_to=row.reply_to,
    )
