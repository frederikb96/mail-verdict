"""
The two model calls the orders worker makes: decide (which order, new, or
none) and write (the order's title, status and summary, from its mails).

The system and user prompts live in config/prompts/orders_*.md.j2,
reproduced verbatim from measurement against real mail -- see
docs/architecture.md's "Orders" section for what that measurement found.
A reworded prompt is not the same prompt: do not "clean up" the wording
in those templates without re-measuring.

Untrusted content (a mail's subject, sender, body, the text of its
attachments, and a candidate's own summary) is JSON-encoded and has every
`<`/`>` replaced by its unicode escape before being placed inside the `<new_mail>`/`<candidates>`/
`<mail n="...">` delimiters below -- the same defence
pipeline/stages/classify.py's _escape_delimiter_breakout uses, so a body
containing the literal string "</new_mail>" cannot close the fence early.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from mail_verdict.core.prompts import load_static_prompt, render_prompt
from mail_verdict.orders.candidates import Candidate
from mail_verdict.orders.carriers import CARRIERS

# --- Escaping, shared by both calls -----------------------------------


def _escape_delimiter_breakout(text: str) -> str:
    return text.replace("<", "\\u003c").replace(">", "\\u003e")


def _json_field(value: Any) -> str:
    """One JSON-encoded string value, angle-bracket-escaped so it can sit
    safely inside an XML-ish delimiter tag."""
    return _escape_delimiter_breakout(json.dumps(value, ensure_ascii=False))


@dataclass(frozen=True)
class PromptDocument:
    """A document as a prompt shows it: its text is already cut to the
    prompt budget and its code markers carry the prompt's document number
    (`[code d2:b1 ...]`). `added` is the day a file was added by hand."""

    index: int
    name: str
    content_type: str
    page_count: int | None
    method: str
    text: str
    added: str = ""


def _document_block(tag: str, document: PromptDocument) -> str:
    pages = f" pages={document.page_count}" if document.page_count else ""
    added = f" added={document.added}" if document.added else ""
    return (
        f"<{tag} n=\"{document.index}\" name={_json_field(document.name)} "
        f"type={_json_field(document.content_type)}{pages} method={document.method}{added}>\n"
        f"{_json_field(document.text)}\n"
        f"</{tag}>"
    )


# --- Decide call --------------------------------------------------------

_IDENTIFIER_KINDS = (
    "order_number", "booking_code", "tracking_number", "invoice_number", "ticket_number",
)


def load_decide_system_prompt() -> str:
    return load_static_prompt("orders_decide_system.md.j2")


def build_decide_schema(handles: list[str], *, allow_none: bool = True) -> dict[str, Any]:
    """The decide answer schema, built per call: `target`'s enum only
    ever offers the handles actually shown in this call's candidate list,
    plus "new" and, for a mail, "none" -- so a wrong handle is
    unrepresentable rather than merely discouraged (observed during design:
    with a free-text field the model wrote the order number into it instead
    of the handle in roughly half the answers that chose a candidate). A
    file the person added on purpose is never "none"."""
    return {
        "type": "object",
        "properties": {
            "mail_kind": {
                "type": "string", "description": "A few words naming what the mail is.",
            },
            "reason": {"type": "string", "description": "One sentence: why this decision."},
            "target": {
                "type": "string", "enum": [*(["none"] if allow_none else []), "new", *handles],
            },
            "identifiers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(_IDENTIFIER_KINDS)},
                        "value": {"type": "string"},
                    },
                    "required": ["kind", "value"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["mail_kind", "reason", "target", "identifiers"],
        "additionalProperties": False,
    }


def validate_decide_response(
    data: dict[str, Any], *, handles: list[str], allow_none: bool = True,
) -> None:
    """Defence in depth, mirroring classify.py's own _validate_shape --
    the schema should already guarantee this."""
    target = data.get("target")
    if target not in {*(["none"] if allow_none else []), "new", *handles}:
        raise ValueError(f"Invalid target {target!r}, expected one of none/new/{handles}")
    if not isinstance(data.get("reason"), str) or not data["reason"]:
        raise ValueError("Missing or empty 'reason' in response")
    if not isinstance(data.get("identifiers"), list):
        raise ValueError("Missing or non-list 'identifiers' in response")


def _format_candidate_block(handle: str, candidate: Candidate) -> str:
    numbers = ", ".join(f"{kind} {value}" for kind, value in candidate.numbers) or "none"
    reasons = "; ".join(candidate.reasons)
    first = (
        candidate.first_mail_at.strftime("%Y-%m-%d %H:%M UTC")
        if candidate.first_mail_at else "unknown"
    )
    last = (
        candidate.last_mail_at.strftime("%Y-%m-%d %H:%M UTC")
        if candidate.last_mail_at else "unknown"
    )
    lines = [
        f"[{handle}] {candidate.merchant}: {candidate.subject} - {candidate.status} "
        f"({'open' if candidate.is_open else 'finished'})",
        f"  mails: {candidate.mail_count}, first {first}, last {last}",
        f"  numbers: {numbers}",
        f"  why listed: {reasons}",
        f"  summary: {_json_field(candidate.summary[:400])}",
        "  latest mails:",
    ]
    for mail in candidate.latest_mails:
        received = mail.received_at.strftime("%Y-%m-%d %H:%M UTC")
        lines.append(
            f"    {received} | {_json_field(mail.from_addr[:50])} | "
            f"{_json_field(mail.subject[:90])}"
        )
    return "\n".join(lines)


def _candidate_blocks(candidates: list[Candidate]) -> str:
    handles = [f"C{i + 1}" for i in range(len(candidates))]
    return (
        "\n".join(
            _format_candidate_block(handle, cand) for handle, cand in zip(handles, candidates)
        )
        if candidates else "(no candidate entries)"
    )


def build_decide_user_prompt(
    *,
    subject: str, from_addr: str, to_addrs: str, received_at: str,
    attachments: list[tuple[str, str]], body: str, candidates: list[Candidate],
    documents: list[PromptDocument] | None = None,
) -> str:
    """
    Args:
        candidates: Already ranked and capped (orders/candidates.py);
            handles C1..Cn are assigned here, in the order given
        documents: The new mail's attachments that carry text or codes,
            already cut to the decide excerpt (orders/documents.py)
    """
    attachment_text = ", ".join(f"{name} ({ctype})" for name, ctype in attachments[:6]) or "none"
    blocks = "".join(f"\n{_document_block('attachment', d)}" for d in documents or [])
    new_mail = (
        f"<new_mail>\n"
        f"received: {received_at}\n"
        f"from: {_json_field(from_addr)}\n"
        f"to: {_json_field(to_addrs)}\n"
        f"subject: {_json_field(subject)}\n"
        f"attachments: {_json_field(attachment_text)}\n"
        f"body: {_json_field(body)}"
        f"{blocks}\n"
        f"</new_mail>"
    )
    return render_prompt(
        "orders_decide_user.md.j2", new_mail=new_mail,
        candidate_blocks=_candidate_blocks(candidates),
    )


def build_decide_file_prompt(
    *, added_at: str, documents: list[PromptDocument], candidates: list[Candidate],
) -> str:
    """The decide prompt for a file added by hand: the same shape as for a
    mail, with no sender or subject."""
    blocks = "".join(f"\n{_document_block('attachment', d)}" for d in documents)
    new_file = f"<new_file>\nadded: {added_at}{blocks}\n</new_file>"
    return render_prompt(
        "orders_decide_user.md.j2", new_mail=new_file,
        candidate_blocks=_candidate_blocks(candidates),
    )


# --- Write call ----------------------------------------------------------

WRITE_ICONS = (
    "package", "ticket", "train", "plane", "bus", "car", "bed", "food",
    "download", "wrench", "receipt",
)
WRITE_KINDS = ("ticket", "order", "other")
FACT_KINDS = (
    "order_number", "booking_code", "ticket_number", "invoice_number", "tracking_number",
    "date", "time", "amount", "place", "seat", "person", "code", "link", "other",
)
# "" for a fact that is not a tracking number.
FACT_CARRIERS = ("", *CARRIERS)
MAX_FACTS = 12

WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "merchant": {"type": "string"},
        "subject": {"type": "string", "description": "At most 60 characters."},
        "status": {"type": "string", "description": "One to five words."},
        "open": {"type": "boolean"},
        "kind": {"type": "string", "enum": list(WRITE_KINDS)},
        "event_start": {
            "type": "string",
            "description": "YYYY-MM-DD the trip, stay or event starts, or ''.",
        },
        "event_end": {
            "type": "string", "description": "YYYY-MM-DD it ends, or ''.",
        },
        "expected_until": {
            "type": "string",
            "description": "YYYY-MM-DD when the entry is naturally over (estimate), or ''.",
        },
        "icon": {"type": "string", "enum": list(WRITE_ICONS)},
        "summary": {"type": "string", "description": "Markdown, at most 1200 characters."},
        "facts": {
            "type": "array",
            "description": f"At most {MAX_FACTS} typed key facts, most important first.",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(FACT_KINDS)},
                    "label": {"type": "string", "description": "At most 30 characters."},
                    "value": {"type": "string"},
                    "carrier": {"type": "string", "enum": list(FACT_CARRIERS)},
                    "code_ref": {
                        "type": "string",
                        "description": "For a code: the marker in the text, e.g. d2:b1. Else ''.",
                    },
                },
                "required": ["kind", "label", "value", "carrier", "code_ref"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "merchant", "subject", "status", "open", "kind", "event_start", "event_end",
        "expected_until", "icon", "summary", "facts",
    ],
    "additionalProperties": False,
}


def validate_write_response(data: dict[str, Any]) -> None:
    if not isinstance(data.get("subject"), str) or not data["subject"]:
        raise ValueError("Missing or empty 'subject' in response")
    if not isinstance(data.get("status"), str) or not data["status"]:
        raise ValueError("Missing or empty 'status' in response")
    if not isinstance(data.get("summary"), str) or not data["summary"]:
        raise ValueError("Missing or empty 'summary' in response")
    if data.get("kind") not in WRITE_KINDS:
        raise ValueError(f"Invalid kind {data.get('kind')!r}, expected one of {WRITE_KINDS}")
    facts = data.get("facts")
    if not isinstance(facts, list):
        raise ValueError("Missing or non-list 'facts' in response")
    for fact in facts:
        if not isinstance(fact, dict) or fact.get("kind") not in FACT_KINDS:
            raise ValueError(f"Invalid fact {fact!r}")
        if not isinstance(fact.get("value"), str) or not isinstance(fact.get("label"), str):
            raise ValueError(f"Fact without label or value: {fact!r}")


def parse_iso_date(value: Any) -> date | None:
    """A date the write call gave (expected_until, event_start, event_end);
    the empty string it uses for "none", and anything that is not an ISO
    date, read as None."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def build_write_system_prompt(*, language: str) -> str:
    return render_prompt("orders_write_system.md.j2", language=language)


def build_write_user_prompt(
    *,
    current_text: dict[str, str] | None,
    mails: list[dict[str, Any]],
    files: list[PromptDocument] | None = None,
) -> str:
    """
    Args:
        current_text: {"merchant", "subject", "status", "summary"} when
            the order has been written before, else None
        mails: Oldest first, each `{"received_at", "from_addr", "subject",
            "body", "documents": [PromptDocument, ...]}` -- body and
            documents already cut to their budgets (orders/documents.py,
            which also decides which mails to include at all)
        files: Files added to the entry by hand, after the mails; document
            numbers run on from the mails' so a code reference names one
            document only
    """
    parts = []
    if current_text is not None:
        parts.append(
            "<current_text>\n"
            f"merchant: {_json_field(current_text['merchant'])}\n"
            f"subject: {_json_field(current_text['subject'])}\n"
            f"status: {_json_field(current_text['status'])}\n"
            f"summary: {_json_field(current_text['summary'])}\n"
            "</current_text>"
        )
    for i, mail in enumerate(mails, start=1):
        names = ", ".join(f"{d.name} ({d.content_type})" for d in mail.get("documents", [])[:6])
        blocks = "".join(f"\n{_document_block('attachment', d)}" for d in mail.get("documents", []))
        parts.append(
            f'<mail n="{i}">\n'
            f"received: {mail['received_at']}\n"
            f"from: {_json_field(mail['from_addr'])}\n"
            f"subject: {_json_field(mail['subject'])}\n"
            f"attachments: {_json_field(names or 'none')}\n"
            f"body: {_json_field(mail['body'])}"
            f"{blocks}\n"
            f"</mail>"
        )
    for document in files or []:
        parts.append(_document_block("file", document))
    return render_prompt("orders_write_user.md.j2", mail_blocks="\n\n".join(parts))
