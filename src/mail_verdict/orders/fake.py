"""
The "fake" provider for orders: deterministic decide/write answers, so
the worker (and everything that exercises it) runs with no API key --
the same role settings.ai.provider == "fake" plays for the classify
stage.
"""

from __future__ import annotations

import re
from typing import Any

from mail_verdict.orders.candidates import Candidate
from mail_verdict.orders.prompts import PromptDocument
from mail_verdict.pipeline.message_view import extract_display_name_and_addr

_ORDER_NUMBER_RE = re.compile(r"\b[A-Z0-9][A-Z0-9-]{4,}\b")
_CODE_REF_RE = re.compile(r"\[code (d\d+:b\d+) ")


def _sender_display_name(from_addr: str) -> str:
    display_name, addr = extract_display_name_and_addr(from_addr)
    return display_name or addr or "Unknown"


def fake_decide(
    *, subject: str, candidates: list[Candidate], allow_none: bool = True,
) -> dict[str, Any]:
    """
    - "none" when the subject contains "newsletter" (never for a file, which
      is `allow_none=False`: "new" then).
    - else the first candidate whose reasons include "same conversation"
      or a "mail contains" number match.
    - else "new".
    """
    handles = [f"C{i + 1}" for i in range(len(candidates))]
    identifiers = [
        {"kind": "order_number", "value": m.group(0)}
        for m in _ORDER_NUMBER_RE.finditer(subject.upper())
        if sum(ch.isdigit() for ch in m.group(0)) >= 3
    ]

    if allow_none and "newsletter" in subject.lower():
        return {
            "mail_kind": "newsletter", "reason": "subject mentions a newsletter",
            "target": "none", "identifiers": [],
        }

    for handle, candidate in zip(handles, candidates):
        if any(
            reason == "same conversation" or reason.startswith("mail contains")
            for reason in candidate.reasons
        ):
            return {
                "mail_kind": "follow-up", "reason": f"matches candidate {handle}",
                "target": handle, "identifiers": identifiers,
            }

    return {
        "mail_kind": "new purchase", "reason": "no candidate matched",
        "target": "new", "identifiers": identifiers,
    }


def fake_write(*, mails: list[dict[str, Any]], files: list[PromptDocument]) -> dict[str, Any]:
    """merchant/subject from the first mail's sender/subject (a file's name
    when there is no mail); one bullet per mail, its subject. The kind
    follows the subject ("ticket", "subscription"), a ticket's event is the
    day of its first mail, every order number in a subject becomes a fact,
    and every code marker in a document becomes a code fact."""
    first = mails[0] if mails else None
    subjects = [m["subject"] for m in mails] or [f.name for f in files]
    joined = " ".join(subjects).lower()
    kind = "ticket" if "ticket" in joined else "other" if "subscription" in joined else "order"
    day = first["received_at"][:10] if first else files[0].added
    facts: list[dict[str, str]] = [
        {"kind": "order_number", "label": "Order number", "value": number,
         "carrier": "", "code_ref": ""}
        for number in dict.fromkeys(
            m.group(0) for subject in subjects for m in _ORDER_NUMBER_RE.finditer(subject.upper())
            if sum(ch.isdigit() for ch in m.group(0)) >= 3
        )
    ]
    documents = [d for mail in mails for d in mail.get("documents", [])] + files
    for document in documents:
        for ref in dict.fromkeys(_CODE_REF_RE.findall(document.text)):
            facts.append({
                "kind": "code", "label": "Ticket code", "value": "", "carrier": "",
                "code_ref": ref,
            })
    return {
        "merchant": _sender_display_name(first["from_addr"]) if first else "Added file",
        "subject": subjects[0][:60],
        "status": "updated",
        "open": True,
        "kind": kind,
        "event_start": day if kind == "ticket" else "",
        "event_end": day if kind == "ticket" else "",
        "expected_until": "",
        "icon": "package",
        "summary": "\n".join(f"- {subject}" for subject in subjects),
        "facts": facts,
    }
