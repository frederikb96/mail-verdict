"""
Validates the orders settings category at write time: the filter patterns
(each one a Python regular expression, checked for compiling before it
can ever reach a live mail) and the model call parameters -- the same
place validate_ai_settings guards the ai category. The model call
parameters are two tasks (decide_ and write_ key groups), each checked
like any other AI task.
"""

from __future__ import annotations

import re
from typing import Any

from mail_verdict.settings.ai_validation import validate_task_model, validate_token_budget

_FILTER_FIELDS = frozenset({"subject", "from", "body"})
_MAX_PATTERN_LENGTH = 300
_MAX_PATTERNS_TOTAL = 300
ORDER_TASKS = ("decide", "write")


def _validate_filter(filter_value: Any) -> None:
    if not isinstance(filter_value, dict):
        raise ValueError("orders.filter must be an object")
    unknown_sections = set(filter_value) - {"include", "exclude"}
    if unknown_sections:
        raise ValueError(f"orders.filter has unknown keys: {sorted(unknown_sections)}")

    total = 0
    for section_name in ("include", "exclude"):
        section = filter_value.get(section_name, {})
        if section == {}:
            continue
        if not isinstance(section, dict):
            raise ValueError(f"orders.filter.{section_name} must be an object")
        unknown_fields = set(section) - _FILTER_FIELDS
        if unknown_fields:
            raise ValueError(
                f"orders.filter.{section_name} has unknown fields: {sorted(unknown_fields)}"
            )
        for field, patterns in section.items():
            if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
                raise ValueError(
                    f"orders.filter.{section_name}.{field} must be a list of strings"
                )
            for pattern in patterns:
                if len(pattern) > _MAX_PATTERN_LENGTH:
                    raise ValueError(
                        f"orders.filter pattern too long ({len(pattern)} characters): "
                        f"{pattern!r}"
                    )
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(
                        f"orders.filter pattern does not compile: {pattern!r}"
                    ) from exc
                total += 1

    if total > _MAX_PATTERNS_TOTAL:
        raise ValueError(
            f"orders.filter has {total} patterns, more than the {_MAX_PATTERNS_TOTAL} allowed"
        )


def validate_orders_settings(effective: dict[str, Any]) -> None:
    """
    Validate a merged orders settings dict as it would read after a write.

    Args:
        effective: The orders settings dict with the incoming partial
            update already merged onto the existing values

    Raises:
        ValueError: If the filter is malformed, a pattern does not
            compile, a task's provider, reasoning effort or max_tokens, or
            auto_close_days/language is out of range
    """
    if "filter" in effective:
        _validate_filter(effective["filter"])

    for task in ORDER_TASKS:
        prefix = f"{task}_"
        validate_task_model(f"orders.{prefix}", effective, prefix=prefix)
        validate_token_budget(f"orders.{prefix}max_tokens", effective.get(f"{prefix}max_tokens"))

    auto_close_days = effective.get("auto_close_days")
    if auto_close_days is not None and not (0 <= int(auto_close_days) <= 3650):
        raise ValueError("orders.auto_close_days must be between 0 and 3650")

    grace_days = effective.get("auto_close_grace_days")
    if grace_days is not None and not (0 <= int(grace_days) <= 3650):
        raise ValueError("orders.auto_close_grace_days must be between 0 and 3650")

    language = effective.get("language")
    if language is not None and not (1 <= len(str(language)) <= 40):
        raise ValueError("orders.language must be 1 to 40 characters")
