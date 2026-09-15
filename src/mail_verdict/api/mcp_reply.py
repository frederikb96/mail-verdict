"""
Server-side reply/forward derivation for the MCP reply_mail tool.

The web UI derives recipients, subject, threading headers and the quoted
original in the browser (ui/src/lib/reply.ts and
ui/src/components/mail/editor/quoted-message-node.ts) -- code an MCP tool,
reached over HTTP with no browser attached, cannot call. This module is
that same set of rules reimplemented in Python, kept close enough that a
reply drafted here reopens in the web UI exactly like one built there: a
quote wrapped in the same div.gmail_quote/blockquote markup
quoted-message-node.ts matches on, and an attribution line embedded
identically in body_text and body_html so DraftEditor's own marker search
(draft-editor.tsx's draftQuotedText) finds it.
"""

from __future__ import annotations

import html
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from mail_verdict.database.models import Identity, Message

_QUOTE_BAR_STYLE = "margin:0 0 0 .8ex;border-left:1px #ccc solid;padding-left:1ex"

_EMAIL_RE = re.compile(r"<([^>]+)>")
_NAME_RE = re.compile(r'^"?([^"<]+)"?\s*<.*>$')
_RE_PREFIX_RE = re.compile(r"^re:", re.IGNORECASE)
_FWD_PREFIX_RE = re.compile(r"^fwd?:", re.IGNORECASE)


def _extract_email(addr: str | None) -> str:
    """The bare address out of "Name <addr>" or a plain address string."""
    if not addr:
        return ""
    match = _EMAIL_RE.search(addr)
    return match.group(1) if match else addr


def _extract_sender_name(addr: str | None) -> str:
    if not addr:
        return "Unknown"
    match = _NAME_RE.match(addr)
    if match:
        return match.group(1).strip()
    return addr.split("@")[0]


def _split_address_header(header: str | None) -> list[str]:
    """One address per entry of a header that may list several -- a comma
    or semicolon inside a quoted display name or inside angle brackets
    separates nothing. Mirrors reply.ts's splitAddressHeader."""
    if not header:
        return []
    entries: list[str] = []
    current = ""
    in_quotes = False
    in_angle = False
    for char in header:
        if char == '"':
            in_quotes = not in_quotes
        elif char == "<" and not in_quotes:
            in_angle = True
        elif char == ">" and not in_quotes:
            in_angle = False
        elif char in (",", ";") and not in_quotes and not in_angle:
            entries.append(current)
            current = ""
            continue
        current += char
    entries.append(current)
    return [e.strip() for e in entries if e.strip()]


def _dedupe_excluding(addrs: list[str], exclude: set[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for addr in addrs:
        key = addr.lower()
        if not addr or key in seen or key in exclude:
            continue
        seen.add(key)
        result.append(addr)
    return result


def merge_addresses(
    base: list[str], extra: list[str] | None, *, exclude: list[str] | None = None,
) -> list[str]:
    """base plus whatever addresses in extra aren't already in it or in
    exclude -- reply_mail's to/cc are additions to the derived recipients,
    never a replacement of them, and its Cc is deduped against its own To
    (pass exclude=to) so an address named in both is not addressed twice.
    Matched on the bare email address, case-insensitively, so "Bob
    <bob@x>" and a bare "bob@x" count as the one address they are."""
    if not extra:
        return base
    exclude_keys = {_extract_email(a).lower() for a in (exclude or [])}
    seen = {_extract_email(a).lower() for a in base} | exclude_keys
    combined = list(base)
    for addr in extra:
        key = _extract_email(addr).lower()
        if addr and key not in seen:
            seen.add(key)
            combined.append(addr)
    return combined


def _subject_with_prefix(subject: str | None) -> str:
    base = subject or "(no subject)"
    return base if _RE_PREFIX_RE.match(base) else f"Re: {base}"


def _format_attribution_date(received_at: datetime | None) -> str:
    """"EEE, d MMM yyyy, HH:mm" -- the reading pane's own full-date format
    (ui/src/lib/format.ts's formatFullDate), in whatever zone received_at
    itself carries (UTC, as stored) rather than a reader's local zone,
    which has no meaning for a message composed here rather than
    displayed in a browser."""
    if received_at is None:
        return ""
    return f"{received_at:%a}, {received_at.day} {received_at:%b %Y, %H:%M}"


def _reply_attribution(source: Message) -> str:
    sender = _extract_sender_name(source.from_addr)
    date = _format_attribution_date(source.received_at)
    return f"On {date}, {sender} wrote:"


def quoted_plain_text(body_text: str | None, attribution: str) -> str:
    """The `> `-prefixed plain-text form of a quote, appended below the
    reply's own authored text to build body_text. Mirrors reply.ts's
    quotedPlainText."""
    body = body_text or ""
    quoted = "\n".join(f"> {line}" for line in body.split("\n"))
    return f"\n\n{attribution}\n{quoted}"


def authored_body_html(body_text: str) -> str:
    """The reply's own typed text as an HTML paragraph, escaped -- the
    counterpart of the editor's markdown-to-HTML export for a body with no
    rich formatting, which is all an MCP caller can send."""
    if not body_text:
        return "<p></p>"
    return "<p>" + "<br>".join(html.escape(line) for line in body_text.splitlines()) + "</p>"


def build_quote_wrapper_html(quote_html: str, attribution: str) -> str:
    """The same div.gmail_quote/gmail_attr/blockquote markup
    quoted-message-node.ts's buildQuotedMessageElement writes -- matching
    it is what lets a drafted reply reopen in the web UI as a proper quote
    rather than inert HTML, and what makes the quote endpoint's own
    round-trip (parseQuotedMessageAttrs, matched on div.gmail_quote) find
    it again. quote_html is expected already sanitized -- see
    api/mails.py's get_message_quote, the same HTML the web UI embeds."""
    attribution_html = "<br>".join(html.escape(line) for line in attribution.split("\n"))
    return (
        '<div class="gmail_quote">'
        f'<div class="gmail_attr">{attribution_html}</div>'
        f'<blockquote type="cite" class="gmail_quote" style="{_QUOTE_BAR_STYLE}">'
        f"{quote_html}"
        "</blockquote>"
        "</div>"
    )


@dataclass
class ReplyDraft:
    to: list[str]
    cc: list[str]
    subject: str
    quoted_text: str
    attribution: str
    in_reply_to: str | None
    references: list[str] | None


@dataclass
class ForwardDraft:
    subject: str
    quoted_text: str
    attribution: str


def derive_reply(
    source: Message, own_addresses: list[str], mode: Literal["reply", "reply_all"],
) -> ReplyDraft:
    """Recipients, subject and threading headers for a reply or
    reply-all, mirroring reply.ts's buildReply. own_addresses are left out
    of a reply-all's Cc, the same reasoning buildReply documents.

    A message this account sent itself (found in Sent, say) is a special
    case reply.ts does not need to handle -- the web UI only ever reaches
    this from an inbound message -- but an MCP caller can name any
    message. Replying to your own mail goes back to whoever you sent it
    to, not to yourself, the same rule Gmail applies."""
    own_set = {a.lower() for a in own_addresses if a}
    sender_email = _extract_email(source.from_addr)
    if sender_email and sender_email.lower() in own_set:
        candidates = [_extract_email(a) for a in (source.to_addrs or [])]
    else:
        reply_to = [_extract_email(a) for a in _split_address_header(source.reply_to)]
        candidates = reply_to if reply_to else ([sender_email] if sender_email else [])
    to = _dedupe_excluding([e for e in candidates if e], own_set)

    cc: list[str] = []
    if mode == "reply_all":
        others = [
            _extract_email(a) for a in [*(source.to_addrs or []), *(source.cc_addrs or [])]
        ]
        exclude = own_set | {a.lower() for a in to}
        cc = _dedupe_excluding(others, exclude)

    references = list(source.msg_references or [])
    if source.message_id:
        references.append(source.message_id)

    attribution = _reply_attribution(source)
    return ReplyDraft(
        to=to,
        cc=cc,
        subject=_subject_with_prefix(source.subject),
        quoted_text=quoted_plain_text(source.body_text, attribution),
        attribution=attribution,
        in_reply_to=source.message_id,
        references=references or None,
    )


def derive_forward(source: Message) -> ForwardDraft:
    """Subject and forwarded-message attribution, mirroring reply.ts's
    buildForward. Carries no In-Reply-To/References -- a forward starts a
    thread of its own with whoever it goes to, the same as buildForward's
    own comment says."""
    base = source.subject or "(no subject)"
    subject = base if _FWD_PREFIX_RE.match(base) else f"Fwd: {base}"
    attribution = "\n".join(
        [
            "---------- Forwarded message ----------",
            f"From: {source.from_addr or 'unknown'}",
            f"Date: {_format_attribution_date(source.received_at)}",
            f"Subject: {source.subject or '(no subject)'}",
            f"To: {', '.join(source.to_addrs or [])}",
        ]
    )
    return ForwardDraft(
        subject=subject,
        quoted_text=quoted_plain_text(source.body_text, attribution),
        attribution=attribution,
    )


def match_identity(addresses: list[str | None], identities: list[Identity]) -> uuid.UUID | None:
    """Which of an account's identities, if any, one of the given
    addresses names -- mirrors identities.ts's matchIdentity, used to pick
    the identity a reply should go out as: whichever alias the original
    was addressed to."""
    by_address = {i.email.lower(): i.id for i in identities}
    for addr in addresses:
        if not addr:
            continue
        match = by_address.get(_extract_email(addr).lower())
        if match:
            return match
    return None
