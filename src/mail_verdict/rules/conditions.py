"""
Rule condition evaluation.

Composable conditions with all/any/not operators.
Each condition type implements a simple predicate against mail context.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MailContext:
    """
    Unified mail context passed to condition evaluators.

    Aggregates mail data from DB model, IMAP state, and enrichment results.
    """

    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    from_addr: str = ""
    to_addrs: list[str] = field(default_factory=list)
    cc_addrs: list[str] = field(default_factory=list)
    raw_headers: dict[str, Any] = field(default_factory=dict)
    size_bytes: int = 0
    has_attachments: bool = False
    attachment_types: list[str] = field(default_factory=list)
    folder: str = ""
    tags: list[str] = field(default_factory=list)
    enrichment_tags: list[str] = field(default_factory=list)


class ConditionEvaluator:
    """
    Evaluates a single condition dict against a MailContext.

    Condition types:
        - subject_contains / body_contains (substring, case-insensitive)
        - subject_matches / body_matches (regex)
        - sender_match (address or domain)
        - sender_domain (domain only)
        - header_match(field, pattern)
        - header_exists(field)
        - size_gt / size_lt (bytes)
        - has_attachment (bool, optional type filter)
        - folder_is(name)
        - tag_is(tag_name)
        - enrichment_tag(tag_name)
    """

    _EVALUATORS: dict[str, str] = {
        "subject_contains": "_eval_subject_contains",
        "body_contains": "_eval_body_contains",
        "subject_matches": "_eval_subject_matches",
        "body_matches": "_eval_body_matches",
        "sender_match": "_eval_sender_match",
        "sender_domain": "_eval_sender_domain",
        "header_match": "_eval_header_match",
        "header_exists": "_eval_header_exists",
        "size_gt": "_eval_size_gt",
        "size_lt": "_eval_size_lt",
        "has_attachment": "_eval_has_attachment",
        "folder_is": "_eval_folder_is",
        "tag_is": "_eval_tag_is",
        "enrichment_tag": "_eval_enrichment_tag",
    }

    def evaluate(self, condition: dict[str, Any], ctx: MailContext) -> bool:
        """
        Evaluate a single condition against mail context.

        Args:
            condition: Condition dict (e.g. {"subject_contains": "invoice"})
            ctx: Mail context to evaluate against

        Returns:
            True if condition matches
        """
        for key, value in condition.items():
            method_name = self._EVALUATORS.get(key)
            if method_name is None:
                logger.warning("Unknown condition type", extra={"type": key})
                return False
            method = getattr(self, method_name)
            return bool(method(value, ctx))
        return False

    def _eval_subject_contains(self, value: str, ctx: MailContext) -> bool:
        """Case-insensitive substring match on subject."""
        return value.lower() in ctx.subject.lower()

    def _eval_body_contains(self, value: str, ctx: MailContext) -> bool:
        """Case-insensitive substring match on body text."""
        return value.lower() in ctx.body_text.lower()

    def _eval_subject_matches(self, pattern: str, ctx: MailContext) -> bool:
        """Regex match on subject."""
        try:
            return bool(re.search(pattern, ctx.subject, re.IGNORECASE))
        except re.error:
            logger.warning("Invalid regex pattern", extra={"pattern": pattern})
            return False

    def _eval_body_matches(self, pattern: str, ctx: MailContext) -> bool:
        """Regex match on body text."""
        try:
            return bool(re.search(pattern, ctx.body_text, re.IGNORECASE))
        except re.error:
            logger.warning("Invalid regex pattern", extra={"pattern": pattern})
            return False

    def _eval_sender_match(self, value: str, ctx: MailContext) -> bool:
        """Match sender address or domain."""
        from_lower = ctx.from_addr.lower()
        value_lower = value.lower()
        if "@" in value_lower:
            return from_lower == value_lower
        return from_lower.endswith(f"@{value_lower}")

    def _eval_sender_domain(self, value: str, ctx: MailContext) -> bool:
        """Match sender domain only."""
        from_lower = ctx.from_addr.lower()
        return from_lower.endswith(f"@{value.lower()}")

    def _eval_header_match(self, value: dict[str, str], ctx: MailContext) -> bool:
        """Match a specific header field against a regex pattern."""
        header_field = value.get("field", "")
        pattern = value.get("pattern", "")
        header_value = str(ctx.raw_headers.get(header_field, ""))
        try:
            return bool(re.search(pattern, header_value, re.IGNORECASE))
        except re.error:
            logger.warning("Invalid regex pattern", extra={"pattern": pattern})
            return False

    def _eval_header_exists(self, value: str, ctx: MailContext) -> bool:
        """Check if a header field exists."""
        return value in ctx.raw_headers

    def _eval_size_gt(self, value: int, ctx: MailContext) -> bool:
        """Check if mail size exceeds threshold in bytes."""
        return ctx.size_bytes > value

    def _eval_size_lt(self, value: int, ctx: MailContext) -> bool:
        """Check if mail size is below threshold in bytes."""
        return ctx.size_bytes < value

    def _eval_has_attachment(self, value: Any, ctx: MailContext) -> bool:
        """Check for attachments, optionally filtering by MIME type."""
        if isinstance(value, str):
            return any(value.lower() in t.lower() for t in ctx.attachment_types)
        return ctx.has_attachments

    def _eval_folder_is(self, value: str, ctx: MailContext) -> bool:
        """Check current folder name."""
        return ctx.folder.lower() == value.lower()

    def _eval_tag_is(self, value: str, ctx: MailContext) -> bool:
        """Check if mail has a specific tag (from Postgres)."""
        return value.lower() in [t.lower() for t in ctx.tags]

    def _eval_enrichment_tag(self, value: str, ctx: MailContext) -> bool:
        """Check if enrichment produced a specific tag."""
        return value.lower() in [t.lower() for t in ctx.enrichment_tags]


def evaluate_condition(
    condition: dict[str, Any],
    ctx: MailContext,
    evaluator: ConditionEvaluator | None = None,
) -> bool:
    """
    Evaluate a condition or composite condition against mail context.

    Supports composition:
        - all: [...] (AND)
        - any: [...] (OR)
        - not: {...} (negate)
        - Single condition shorthand (just the condition dict)

    Args:
        condition: Condition dict, possibly with all/any/not composition
        ctx: Mail context
        evaluator: Optional evaluator instance (creates one if not provided)

    Returns:
        True if condition matches
    """
    if evaluator is None:
        evaluator = ConditionEvaluator()

    # Composite: all (AND)
    if "all" in condition:
        return all(evaluate_condition(c, ctx, evaluator) for c in condition["all"])

    # Composite: any (OR)
    if "any" in condition:
        return any(evaluate_condition(c, ctx, evaluator) for c in condition["any"])

    # Composite: not (negate)
    if "not" in condition:
        return not evaluate_condition(condition["not"], ctx, evaluator)

    # Single condition
    return evaluator.evaluate(condition, ctx)
