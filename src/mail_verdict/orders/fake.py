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
from mail_verdict.pipeline.message_view import extract_display_name_and_addr

_ORDER_NUMBER_RE = re.compile(r"\b[A-Z0-9][A-Z0-9-]{4,}\b")


def _sender_display_name(from_addr: str) -> str:
    display_name, addr = extract_display_name_and_addr(from_addr)
    return display_name or addr or "Unknown"


def fake_decide(
    *, subject: str, candidates: list[Candidate],
) -> dict[str, Any]:
    """
    - "none" when the subject contains "newsletter".
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

    if "newsletter" in subject.lower():
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


def fake_write(*, mails: list[dict[str, Any]]) -> dict[str, Any]:
    """merchant/subject from the first mail's sender/subject; one bullet
    per mail, its subject."""
    first = mails[0]
    bullets = "\n".join(f"- {m['subject']}" for m in mails)
    return {
        "merchant": _sender_display_name(first["from_addr"]),
        "subject": first["subject"][:60],
        "status": "updated",
        "open": True,
        "icon": "package",
        "summary": bullets,
    }
