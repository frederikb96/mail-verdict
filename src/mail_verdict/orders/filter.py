"""
The orders first filter: a cheap deterministic pattern match that lets
through anything that might be an order, ticket or booking, before any
model is ever called. Read fresh from settings.orders.filter on every
call (the pipeline stage's own execute, and the catch-up sweep) -- a
pattern change takes effect on the next mail, no restart.

Deliberately generous: recall matters far more than precision here, since
a false positive costs one cheap decide call and a false negative is a
mail the register never sees at all. The decide call (orders/prompts.py)
is what actually separates a real purchase from a newsletter using the
word "order".
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# The body field a filter pattern is checked against is capped to this
# many characters -- matching the excerpt orders/content.py's prepare_body
# produces, so "does this pattern match the body" means the same thing
# here as it does to the model reading the same text.
_FILTER_BODY_CHARS = 4_000

_FIELD_ORDER = ("subject", "from", "body")


@dataclass(frozen=True)
class FilterResult:
    """Whether a mail passes the first filter, and why."""

    passed: bool
    reason: str | None = None


def _compile_patterns(raw: Sequence[str]) -> list[tuple[str, re.Pattern[str]]]:
    compiled = []
    for pattern in raw:
        try:
            compiled.append((pattern, re.compile(pattern, re.IGNORECASE)))
        except re.error:
            # A pattern that fails to compile here was accepted once and
            # has since become unparseable some other way (never through
            # this application's own write path, which validates every
            # pattern at write time -- see settings/orders_validation.py).
            # Skipped rather than raised, so one bad pattern in a hand-
            # edited settings row never takes the whole filter down.
            continue
    return compiled


def evaluate_filter(
    *, subject: str, from_header: str, body: str, patterns: Mapping[str, Any],
) -> FilterResult:
    """
    Evaluate the pattern half of the first filter -- never the thread/
    number bypass rules, which need a database lookup and live in
    orders/lookup.py's OrderLookup instead.

    Args:
        subject: The message's Subject header
        from_header: The message's whole From header (display name and
            address)
        body: The prepared body (orders/content.py's prepare_body), first
            _FILTER_BODY_CHARS characters read
        patterns: settings.orders.filter, `{"include": {...}, "exclude":
            {...}}`

    Returns:
        FilterResult(passed=True, reason="<field>:<pattern>") for the
        first include pattern that matched, checked in the order subject,
        from, body; FilterResult(passed=False) when no include pattern
        matched or an exclude pattern did
    """
    fields = {
        "subject": subject or "", "from": from_header or "",
        "body": (body or "")[:_FILTER_BODY_CHARS],
    }

    exclude = patterns.get("exclude", {}) or {}
    for field in _FIELD_ORDER:
        for _pattern_text, compiled in _compile_patterns(exclude.get(field, []) or []):
            if compiled.search(fields[field]):
                return FilterResult(passed=False)

    include = patterns.get("include", {}) or {}
    for field in _FIELD_ORDER:
        for pattern_text, compiled in _compile_patterns(include.get(field, []) or []):
            if compiled.search(fields[field]):
                return FilterResult(passed=True, reason=f"{field}:{pattern_text}")

    return FilterResult(passed=False)
