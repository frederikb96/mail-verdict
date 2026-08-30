"""
Pipeline definitions: an append-only revision history, never a row updated
in place.

The current definition is `max(revision)`. A misconfigured stage edit is
recoverable by inspection ("what did it look like before this revision")
without a separate audit table -- the revisions themselves are the audit
trail. Building and storing the very first revision is also where an
existing deployment's `settings.rules` and `spam.*` settings become a
pipeline definition; see `build_migrated_definition`.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from mail_verdict.pipeline.contracts import JsonValue, StageDefinition
from mail_verdict.pipeline.effect_codec import EffectConfigError, effect_to_dict, parse_effect

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

# Guards the read-current-then-insert in append() below. Distinct from
# every other advisory-lock key in the process (queue/manager.py's
# reclaim timer at 761_034_221, embeddings/worker.py's backfill lock at
# 761_034_222, pipeline/enqueue.py's reconciliation timer at 761_034_331).
_APPEND_LOCK_KEY = 761_034_402

# Only mail.received rules migrate into a stage -- the pipeline is
# enqueued on arrival only (see pipeline/enqueue.py), so a rule keyed to
# a move or a delete trigger has nothing left to fire it, and reacting to
# a move belongs to the stateless feedback listener in spam/feedback.py,
# never to a stage that could loop on its own writes.
_MIGRATABLE_TRIGGER = "mail.received"

_ACTION_TO_EFFECT_KIND: dict[str, str] = {
    "move_to": "move", "trash": "trash", "move_to_spam": "move",
    "mark_as": "set_flags", "star": "set_flags", "unstar": "set_flags",
    "tag": "tag", "remove_tag": "tag", "notify": "notify",
}


@dataclass(frozen=True)
class PipelineDefinition:
    """One revision's parsed document."""

    revision: int
    enabled: bool
    stages: tuple[StageDefinition, ...]


class StaleRevisionError(Exception):
    """`append`'s `expected_base_revision` no longer matches -- the API's
    409. Carries both numbers so a caller can report them without a
    second read."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"base_revision {expected} is stale -- current revision is {actual}")
        self.expected = expected
        self.actual = actual


class PipelineRevisionRepository:
    """CRUD over `pipeline_revisions` -- append-only, current = max(revision)."""

    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    async def current(self) -> PipelineDefinition | None:
        """The most recent revision, or None if no revision has ever been written."""
        async with self._db.session() as session:
            result = await session.execute(
                text(
                    "SELECT revision, document FROM pipeline_revisions "
                    "ORDER BY revision DESC LIMIT 1"
                )
            )
            row = result.one_or_none()
        if row is None:
            return None
        return _parse_document(row.revision, row.document)

    async def get(self, revision: int) -> PipelineDefinition | None:
        """One specific revision's document, or None if it does not
        exist -- the source a restore reads from."""
        async with self._db.session() as session:
            result = await session.execute(
                text("SELECT revision, document FROM pipeline_revisions WHERE revision = :r"),
                {"r": revision},
            )
            row = result.one_or_none()
        if row is None:
            return None
        return _parse_document(row.revision, row.document)

    async def append(
        self,
        document: dict[str, Any],
        *,
        note: str | None = None,
        expected_base_revision: int | None = None,
    ) -> int:
        """
        Append a new revision, returning its revision number.

        Args:
            document: `{"enabled": bool, "stages": [...]}`
            note: Free-text, shown in the revision history
            expected_base_revision: If given, the revision the caller's
                edit was computed against. Checked against the current
                revision and the insert performed in the same
                transaction, serialized by an advisory lock held for
                its duration -- reading the current revision in one
                session and appending in another (a caller checking
                separately, then calling this with no expectation) lets
                two writers who read the same base both pass and both
                append, with the later one silently winning and the
                other writer's edit lost. Postgres gives no way to lock
                an aggregate query directly, which is why this takes a
                lock rather than `SELECT ... FOR UPDATE`.

        Returns:
            The new revision number

        Raises:
            StaleRevisionError: `expected_base_revision` no longer
                matches the current revision
        """
        async with self._db.session() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _APPEND_LOCK_KEY},
            )
            if expected_base_revision is not None:
                current_result = await session.execute(
                    text("SELECT revision FROM pipeline_revisions ORDER BY revision DESC LIMIT 1")
                )
                current_revision = current_result.scalar_one_or_none() or 0
                if current_revision != expected_base_revision:
                    raise StaleRevisionError(expected_base_revision, current_revision)
            result = await session.execute(
                text(
                    "INSERT INTO pipeline_revisions (document, note) "
                    "VALUES (CAST(:document AS jsonb), :note) RETURNING revision"
                ),
                {"document": json.dumps(document), "note": note},
            )
            return int(result.scalar_one())

    async def list_revisions(self) -> list[dict[str, Any]]:
        """Every revision, newest first -- the history the API surfaces."""
        async with self._db.session() as session:
            result = await session.execute(
                text(
                    "SELECT revision, note, created_at FROM pipeline_revisions "
                    "ORDER BY revision DESC"
                )
            )
            return [
                {"revision": row.revision, "note": row.note, "created_at": row.created_at}
                for row in result.all()
            ]


def _parse_document(revision: int, document: dict[str, Any]) -> PipelineDefinition:
    stages = tuple(
        StageDefinition(
            stage_id=s["stage_id"], type=s["type"], name=s.get("name", s["stage_id"]),
            config=s.get("config", {}), enabled=s.get("enabled", True),
            halt=s.get("halt", False),
            accounts=(
                tuple(uuid.UUID(a) for a in s["accounts"]) if s.get("accounts") else None
            ),
        )
        for s in document.get("stages", [])
    )
    return PipelineDefinition(
        revision=revision, enabled=document.get("enabled", True), stages=stages,
    )


def definition_to_document(definition: PipelineDefinition) -> dict[str, JsonValue]:
    """The inverse of `_parse_document` -- used when writing a new revision
    derived from the current one (e.g. a restore)."""
    return {
        "enabled": definition.enabled,
        "stages": [
            {
                "stage_id": s.stage_id, "type": s.type, "name": s.name,
                "config": dict(s.config), "enabled": s.enabled, "halt": s.halt,
                "accounts": [str(a) for a in s.accounts] if s.accounts else None,
            }
            for s in definition.stages
        ],
    }


def build_migrated_definition(
    *, raw_rules: list[dict[str, Any]], spam_settings: dict[str, Any],
) -> dict[str, Any]:
    """
    Build the first pipeline revision's document from an existing
    deployment's `settings.rules` and `spam.*` settings.

    Args:
        raw_rules: The "rules" settings category's "rules" list
        spam_settings: The "spam" settings category

    Returns:
        A document ready for `PipelineRevisionRepository.append`
    """
    stages: list[dict[str, Any]] = []

    classify_stage: dict[str, Any] = {
        "stage_id": "classify", "type": "classify", "name": "Classify spam",
        "config": {}, "enabled": bool(spam_settings.get("enabled", True)), "halt": False,
    }
    stages.append(classify_stage)

    for i, raw in enumerate(raw_rules):
        migrated = _migrate_rule(raw, index=i)
        if migrated is not None:
            stages.append(migrated)

    if bool(spam_settings.get("auto_move_to_junk", True)):
        effects: list[dict[str, Any]] = [{"move": {"special_use": "junk"}}]
        if bool(spam_settings.get("auto_mark_read", True)):
            effects.append({"set_flags": {"seen": True}})
        move_spam_stage: dict[str, Any] = {
            "stage_id": "move-spam", "type": "match", "name": "Move spam to junk",
            "config": {"when": {"verdict_is": "spam"}, "effects": effects},
            "enabled": True, "halt": False,
        }
        stages.append(move_spam_stage)

    return {"enabled": True, "stages": stages}


def _migrate_rule(raw: dict[str, Any], *, index: int) -> dict[str, Any] | None:
    """
    Migrate one `settings.rules` entry to a `match` stage, or None if it
    cannot be expressed in the new pipeline (a trigger other than
    mail.received -- see `_MIGRATABLE_TRIGGER`).
    """
    trigger = raw.get("trigger", "")
    name = raw.get("name") or f"rule-{index}"
    if trigger != _MIGRATABLE_TRIGGER:
        logger.warning(
            "Rule not migrated -- trigger no longer exists in the pipeline",
            extra={"rule": name, "trigger": trigger},
        )
        return None

    conditions = raw.get("conditions", {})
    if isinstance(conditions, list):
        conditions = {"all": conditions} if len(conditions) != 1 else conditions[0]

    effects: list[dict[str, Any]] = []
    halt = False
    for action in raw.get("actions", []):
        if not isinstance(action, dict) or len(action) != 1:
            continue
        (kind, value), = action.items()
        if kind == "stop":
            halt = True
            break
        if kind in ("copy_to", "forward_to"):
            logger.warning(
                "Action not migrated -- never implemented in PostIMAP mode",
                extra={"rule": name, "action": kind},
            )
            continue
        effect = _migrate_action(kind, value)
        if effect is not None:
            try:
                parse_effect(effect)
            except EffectConfigError:
                logger.warning(
                    "Action did not migrate cleanly", extra={"rule": name, "action": kind},
                )
                continue
            effects.append(effect)

    return {
        "stage_id": f"rule-{index}-{name}"[:63], "type": "match", "name": name,
        "config": {"when": conditions, "effects": effects}, "enabled": True, "halt": halt,
    }


def _migrate_action(kind: str, value: Any) -> dict[str, Any] | None:
    """One old-rule action dict entry to one new effect dict, or None if
    the action type has no successor (see `_ACTION_TO_EFFECT_KIND`)."""
    if kind == "move_to":
        return {"move": {"folder_name": value}}
    if kind == "move_to_spam":
        return {"move": {"special_use": "junk"}}
    if kind == "trash":
        return {"trash": {}}
    if kind == "mark_as":
        return {"set_flags": {"seen": value == "read"}}
    if kind == "star":
        return {"set_flags": {"flagged": True}}
    if kind == "unstar":
        return {"set_flags": {"flagged": False}}
    if kind == "tag":
        return {"tag": {"add": [value]}}
    if kind == "remove_tag":
        return {"tag": {"remove": [value]}}
    if kind == "notify":
        text_value = value if isinstance(value, str) else str(value)
        return {"notify": {"text": text_value}}
    logger.warning("Unknown action type, not migrated", extra={"action": kind})
    return None


__all__ = [
    "PipelineDefinition",
    "PipelineRevisionRepository",
    "StaleRevisionError",
    "build_migrated_definition",
    "definition_to_document",
    "effect_to_dict",
]
