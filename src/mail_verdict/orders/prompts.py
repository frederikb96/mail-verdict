"""
The two model calls the orders worker makes: decide (which order, new, or
none) and write (the order's title, status and summary, from its mails).

The system and user prompts live in config/prompts/orders_*.md.j2,
reproduced verbatim from measurement against real mail -- see
docs/architecture.md's "Orders" section for what that measurement found.
A reworded prompt is not the same prompt: do not "clean up" the wording
in those templates without re-measuring.

Untrusted content (a mail's subject, sender, body, and a candidate's own
summary) is JSON-encoded and has every `<`/`>` replaced by its unicode
escape before being placed inside the `<new_mail>`/`<candidates>`/
`<mail n="...">` delimiters below -- the same defence
pipeline/stages/classify.py's _escape_delimiter_breakout uses, so a body
containing the literal string "</new_mail>" cannot close the fence early.
"""

from __future__ import annotations

import json
from typing import Any

from mail_verdict.core.prompts import load_static_prompt, render_prompt
from mail_verdict.orders.candidates import Candidate

# --- Escaping, shared by both calls -----------------------------------


def _escape_delimiter_breakout(text: str) -> str:
    return text.replace("<", "\\u003c").replace(">", "\\u003e")


def _json_field(value: Any) -> str:
    """One JSON-encoded string value, angle-bracket-escaped so it can sit
    safely inside an XML-ish delimiter tag."""
    return _escape_delimiter_breakout(json.dumps(value, ensure_ascii=False))


# --- Decide call --------------------------------------------------------

_IDENTIFIER_KINDS = (
    "order_number", "booking_code", "tracking_number", "invoice_number", "ticket_number",
)


def load_decide_system_prompt() -> str:
    return load_static_prompt("orders_decide_system.md.j2")


def build_decide_schema(handles: list[str]) -> dict[str, Any]:
    """The decide answer schema, built per call: `target`'s enum only
    ever offers the handles actually shown in this call's candidate list,
    plus "none" and "new" -- so a wrong handle is unrepresentable rather
    than merely discouraged (observed during design: with a free-text
    field the model wrote the order number into it instead of the handle
    in roughly half the answers that chose a candidate)."""
    return {
        "type": "object",
        "properties": {
            "mail_kind": {
                "type": "string", "description": "A few words naming what the mail is.",
            },
            "reason": {"type": "string", "description": "One sentence: why this decision."},
            "target": {"type": "string", "enum": ["none", "new", *handles]},
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


def validate_decide_response(data: dict[str, Any], *, handles: list[str]) -> None:
    """Defence in depth, mirroring classify.py's own _validate_shape --
    the schema should already guarantee this."""
    target = data.get("target")
    if target not in {"none", "new", *handles}:
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


def build_decide_user_prompt(
    *,
    subject: str, from_addr: str, to_addrs: str, received_at: str,
    attachments: list[tuple[str, str]], body: str, candidates: list[Candidate],
) -> str:
    """
    Args:
        candidates: Already ranked and capped (orders/candidates.py);
            handles C1..Cn are assigned here, in the order given
    """
    handles = [f"C{i + 1}" for i in range(len(candidates))]
    candidate_blocks = (
        "\n".join(
            _format_candidate_block(handle, cand) for handle, cand in zip(handles, candidates)
        )
        if candidates else "(no candidate entries)"
    )
    attachment_text = ", ".join(f"{name} ({ctype})" for name, ctype in attachments[:6]) or "none"
    new_mail = (
        f"<new_mail>\n"
        f"received: {received_at}\n"
        f"from: {_json_field(from_addr)}\n"
        f"to: {_json_field(to_addrs)}\n"
        f"subject: {_json_field(subject)}\n"
        f"attachments: {_json_field(attachment_text)}\n"
        f"body: {_json_field(body[:3000])}\n"
        f"</new_mail>"
    )
    return render_prompt(
        "orders_decide_user.md.j2", new_mail=new_mail, candidate_blocks=candidate_blocks,
    )


# --- Write call ----------------------------------------------------------

WRITE_ICONS = (
    "package", "ticket", "train", "plane", "bus", "car", "bed", "food",
    "download", "wrench", "receipt",
)

WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "merchant": {"type": "string"},
        "subject": {"type": "string", "description": "At most 60 characters."},
        "status": {"type": "string", "description": "One to five words."},
        "open": {"type": "boolean"},
        "icon": {"type": "string", "enum": list(WRITE_ICONS)},
        "summary": {"type": "string", "description": "Markdown, at most 900 characters."},
    },
    "required": ["merchant", "subject", "status", "open", "icon", "summary"],
    "additionalProperties": False,
}


def validate_write_response(data: dict[str, Any]) -> None:
    if not isinstance(data.get("subject"), str) or not data["subject"]:
        raise ValueError("Missing or empty 'subject' in response")
    if not isinstance(data.get("status"), str) or not data["status"]:
        raise ValueError("Missing or empty 'status' in response")
    if not isinstance(data.get("summary"), str) or not data["summary"]:
        raise ValueError("Missing or empty 'summary' in response")


def build_write_system_prompt(*, language: str) -> str:
    return render_prompt("orders_write_system.md.j2", language=language)


def build_write_user_prompt(
    *,
    current_text: dict[str, str] | None,
    mails: list[dict[str, Any]],
) -> str:
    """
    Args:
        current_text: {"merchant", "subject", "status", "summary"} when
            the order has been written before, else None
        mails: Oldest first, each `{"received_at", "from_addr", "subject",
            "attachments": [(name, type), ...], "body", "body_chars"}` --
            body already cut by the caller to the per-mail budget (3000
            for the newest of at most 12, 1500 otherwise; see orders/
            worker.py, which decides which mails to include at all)
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
        attachment_text = ", ".join(
            f"{name} ({ctype})" for name, ctype in mail.get("attachments", [])[:6]
        ) or "none"
        parts.append(
            f'<mail n="{i}">\n'
            f"received: {mail['received_at']}\n"
            f"from: {_json_field(mail['from_addr'])}\n"
            f"subject: {_json_field(mail['subject'])}\n"
            f"attachments: {_json_field(attachment_text)}\n"
            f"body: {_json_field(mail['body'][: mail['body_chars']])}\n"
            f"</mail>"
        )
    return render_prompt("orders_write_user.md.j2", mail_blocks="\n\n".join(parts))
