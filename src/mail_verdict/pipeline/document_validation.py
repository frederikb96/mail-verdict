"""
Hard validation for a pipeline document before it becomes a revision.

Two kinds of problem, and the write endpoints in api/pipeline.py treat
them differently on purpose. A syntax error, an unknown stage type, an
unknown effect, an unknown condition type, a condition leaf with more
than one key, or a duplicate stage name can never become valid on their
own -- these are rejected outright (400), collected all at once rather
than stopping at the first one, since fixing them one HTTP round trip at
a time is not a client's job. A folder reference that does not currently
resolve is a different kind of problem entirely and is never rejected
here -- see pipeline/health.py, which is where it belongs: folders
appear asynchronously, so a stage referencing one that does not exist
yet may still be correct.
"""

from __future__ import annotations

import uuid
from typing import Any

from mail_verdict.pipeline.contracts import StageDefinition, StageMisconfigured
from mail_verdict.pipeline.registry import build_stage
from mail_verdict.rules.conditions import KNOWN_CONDITION_TYPES


class DocumentValidationError(ValueError):
    """One or more hard-validation problems, collected together."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def validate_document(document: dict[str, Any]) -> list[StageDefinition]:
    """
    Hard-validate a raw pipeline document (`{"enabled": bool, "stages":
    [...]}`) and return its parsed stage definitions.

    Args:
        document: The document as the API received it

    Returns:
        Parsed, individually construction-checked stage definitions, in
        the order given

    Raises:
        DocumentValidationError: every problem found, not just the first
    """
    problems: list[str] = []
    stages_raw = document.get("stages")
    if not isinstance(stages_raw, list):
        raise DocumentValidationError(["'stages' must be a list"])

    seen_ids: set[str] = set()
    definitions: list[StageDefinition] = []
    for i, raw in enumerate(stages_raw):
        if not isinstance(raw, dict):
            problems.append(f"stage {i}: must be an object")
            continue

        stage_id = raw.get("stage_id")
        if not stage_id or not isinstance(stage_id, str):
            problems.append(f"stage {i}: 'stage_id' is required and must be a string")
            continue
        if stage_id in seen_ids:
            problems.append(f"stage {stage_id!r}: duplicate stage_id")
            continue
        seen_ids.add(stage_id)

        stage_type = raw.get("type")
        if not stage_type or not isinstance(stage_type, str):
            problems.append(f"stage {stage_id!r}: 'type' is required and must be a string")
            continue

        accounts_raw = raw.get("accounts")
        try:
            accounts = tuple(uuid.UUID(a) for a in accounts_raw) if accounts_raw else None
        except (ValueError, TypeError):
            problems.append(f"stage {stage_id!r}: 'accounts' must be a list of UUIDs")
            continue

        definition = StageDefinition(
            stage_id=stage_id, type=stage_type, name=str(raw.get("name") or stage_id),
            config=raw.get("config", {}) or {}, enabled=bool(raw.get("enabled", True)),
            halt=bool(raw.get("halt", False)), accounts=accounts,
        )

        try:
            build_stage(definition)
        except StageMisconfigured as exc:
            problems.append(str(exc))
            continue

        when = definition.config.get("when") if isinstance(definition.config, dict) else None
        if when:
            problems.extend(_validate_condition_tree(when, stage_id=stage_id))

        definitions.append(definition)

    if problems:
        raise DocumentValidationError(problems)
    return definitions


def _validate_condition_tree(condition: Any, *, stage_id: str) -> list[str]:
    """Recursively check a `when` tree's leaf keys against the closed
    condition vocabulary -- never executed, only shaped-checked; a bad
    regex or an always-false comparison is a runtime concern, not a
    write-time one."""
    if not isinstance(condition, dict) or not condition:
        return [f"stage {stage_id!r}: condition must be a non-empty object"]

    if "all" in condition or "any" in condition:
        key = "all" if "all" in condition else "any"
        items = condition[key]
        if not isinstance(items, list) or not items:
            return [f"stage {stage_id!r}: {key!r} must be a non-empty list"]
        problems: list[str] = []
        for item in items:
            problems.extend(_validate_condition_tree(item, stage_id=stage_id))
        return problems

    if "not" in condition:
        return _validate_condition_tree(condition["not"], stage_id=stage_id)

    unknown = [key for key in condition if key not in KNOWN_CONDITION_TYPES]
    if unknown:
        return [f"stage {stage_id!r}: unknown condition type {unknown[0]!r}"]
    # rules/conditions.py's evaluator takes exactly one key per leaf --
    # {"subject_contains": "x", "sender_domain": "y"} reads as AND to
    # anyone looking at it, but nothing evaluates it that way, so it is
    # rejected here rather than silently matching on "x" alone. "all" is
    # the vocabulary's one way to combine conditions.
    if len(condition) > 1:
        return [
            f"stage {stage_id!r}: a condition may only have one key, got "
            f"{sorted(condition)!r} -- use 'all' to combine conditions"
        ]
    return []


__all__ = ["DocumentValidationError", "validate_document"]
