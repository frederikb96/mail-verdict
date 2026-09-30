"""
MessageView: the immutable, narrow snapshot a stage actually sees.

Loaded fresh at run execution time, never carried on the queue row (see
pipeline/runner.py) -- the queue row only ever holds (account_id, msg_key).

The loader selects a fixed, short column list. It never selects
raw_source (the full RFC822 bytea) and never selects a whole Attachment
row -- rules/engine.py's context builder did both, which at pipeline
concurrency is an out-of-memory pod restart in the middle of a backfill
that looks like anything but its actual cause.

`body` is bounded to _BODY_EXCERPT_CHARS, long enough to judge tone and
intent, short enough that a 20MB message costs nothing to load -- but
every URL the message actually links to or mentions is appended
regardless of where in the body it fell, since a link is the strongest
spam signal a message carries and a naive prefix cut would otherwise drop
whichever ones happen to sit past the cut, or hide entirely behind an
HTML anchor's visible text (see _extract_urls/_append_missing_urls).
"""

from __future__ import annotations

import re
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

# A second, larger excerpt of the RAW (unstripped, un-URL-rewritten) body
# text and HTML -- what the orders stage (pipeline/stages/orders.py) runs
# its own body preparation over (orders/content.py's prepare_body), since
# that preparation and the classify stage's excerpt above serve different
# purposes and must not share one truncation. Bounded, the same reasoning
# as _BODY_EXCERPT_CHARS: body_text/body_html are already fully loaded
# into memory by this same query regardless, so exposing a bounded slice
# costs nothing extra to compute -- only unbounded retention across a
# whole run would reintroduce the OOM-under-concurrency risk this
# module's docstring warns about.
_ORDERS_BODY_EXCERPT_CHARS = 20_000

# A bare URL, in either plain text or the text nh3.clean(tags=set()) below
# leaves behind. Trailing characters a sentence or a closing bracket
# commonly glues on are stripped by _clean_url rather than excluded here,
# since a greedy \S+ has no other way to know where a URL actually ends.
_URL_RE = re.compile(r'https?://[^\s<>"\')\]]+', re.IGNORECASE)
# An anchor's real target -- what nh3.clean(tags=set()) discards along
# with the rest of the markup, so "Click here to verify your account"
# linking to a phishing site would otherwise never reach the model at all.
_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}'\""
# A newsletter's link-heavy footer must not blow the excerpt out on its
# own -- capped, first-seen order.
_MAX_URLS = 20


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
    thread_id: uuid.UUID | None = None
    # Raw (unstripped) body, bounded to _ORDERS_BODY_EXCERPT_CHARS -- for
    # the orders stage's own body preparation only (orders/content.py's
    # prepare_body); every other stage reads `body` above.
    body_text_raw: str | None = None
    body_html_raw: str | None = None

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


def _clean_url(url: str) -> str:
    """Trim whatever a sentence or a closing bracket commonly glues onto
    the end of a URL a regex greedily matched past its real end."""
    return url.rstrip(_URL_TRAILING_PUNCTUATION)


def _extract_urls(*, body_text: str | None, body_html: str | None) -> tuple[str, ...]:
    """
    Every http(s) URL this message actually links to or mentions -- the
    best spam signal a message carries, so it has to reach the model
    however the body itself is trimmed (see _append_missing_urls).

    An HTML body is scanned twice: once for `href` targets, since
    nh3.clean(tags=set()) (_strip_html) discards them along with the rest
    of the markup and a "Click here" anchor would otherwise arrive with no
    URL at all, and once for a bare URL typed directly into the markup
    rather than wrapped in an anchor. A plain-text body only ever needs
    the bare-URL pass.

    Returns:
        Deduplicated, first-seen order, capped at _MAX_URLS
    """
    seen: dict[str, None] = {}
    if body_html:
        for match in _HREF_RE.finditer(body_html):
            url = _clean_url(match.group(1))
            if url.lower().startswith(("http://", "https://")):
                seen.setdefault(url, None)
        for match in _URL_RE.finditer(body_html):
            seen.setdefault(_clean_url(match.group(0)), None)
    if body_text:
        for match in _URL_RE.finditer(body_text):
            seen.setdefault(_clean_url(match.group(0)), None)
    return tuple(seen)[:_MAX_URLS]


def _append_missing_urls(excerpt: str, urls: tuple[str, ...]) -> str:
    """
    Append whichever of `urls` the kept excerpt doesn't already show
    verbatim -- covering both a URL trimmed off by _BODY_EXCERPT_CHARS's
    own cut and a link whose target never appeared in the excerpt's text
    at all (an HTML anchor's href, see _extract_urls).

    The excerpt itself is trimmed to make room so the combined result
    never exceeds _BODY_EXCERPT_CHARS -- a fixed ceiling on what reaches
    the model regardless of how many links a message carries, and the
    reason this always wins over losing the tail of the excerpt to a URL
    list that would otherwise grow the body past the model's context
    budget uncapped.
    """
    missing = [url for url in urls if url not in excerpt]
    if not missing:
        return excerpt
    suffix = "\n[links in this message: " + ", ".join(missing) + "]"
    budget = max(_BODY_EXCERPT_CHARS - len(suffix), 0)
    return excerpt[:budget] + suffix


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
            Message.thread_id,
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
    body = _append_missing_urls(
        body, _extract_urls(body_text=row.body_text, body_html=row.body_html),
    )

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
        thread_id=row.thread_id,
        body_text_raw=row.body_text[:_ORDERS_BODY_EXCERPT_CHARS] if row.body_text else None,
        body_html_raw=row.body_html[:_ORDERS_BODY_EXCERPT_CHARS] if row.body_html else None,
    )
