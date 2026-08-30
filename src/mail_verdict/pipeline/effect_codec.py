"""
JSON encoding for effects -- one key per effect type, matching the shape a
stage's `config.effects` list and a run's stored trace both use.

    {"move": {"special_use": "junk"}}
    {"set_flags": {"seen": true}}
    {"record_verdict": {"is_spam": true, "reasoning": "...", "model": "..."}}

A single definition here is what keeps a stage's configured effects and a
run's recorded trace speaking the same vocabulary -- encoding drift
between the two would make "what did this stage do" unanswerable from the
stored trace alone.
"""

from __future__ import annotations

import uuid
from typing import Any

from mail_verdict.pipeline.contracts import (
    Effect,
    Expunge,
    Keywords,
    Move,
    Notify,
    RecordVerdict,
    SetFlags,
    Tag,
    Trash,
)


class EffectConfigError(ValueError):
    """An effect dict does not name a known effect type or is malformed."""


def parse_effect(raw: dict[str, Any]) -> Effect:
    """Parse one `{type: value}` dict into an Effect."""
    if len(raw) != 1:
        raise EffectConfigError(f"effect dict must have exactly one key, got {raw!r}")
    (kind, value), = raw.items()
    value = value or {}

    if kind == "move":
        folder_id = value.get("folder_id")
        return Move(
            folder_name=value.get("folder_name"),
            special_use=value.get("special_use"),
            folder_id=uuid.UUID(folder_id) if folder_id else None,
        )
    if kind == "trash":
        return Trash()
    if kind == "expunge":
        return Expunge()
    if kind == "set_flags":
        return SetFlags(
            seen=value.get("seen"), flagged=value.get("flagged"),
            answered=value.get("answered"), deleted=value.get("deleted"),
        )
    if kind == "keywords":
        return Keywords(add=tuple(value.get("add", ())), remove=tuple(value.get("remove", ())))
    if kind == "tag":
        return Tag(add=tuple(value.get("add", ())), remove=tuple(value.get("remove", ())))
    if kind == "record_verdict":
        return RecordVerdict(
            is_spam=bool(value["is_spam"]), reasoning=str(value.get("reasoning", "")),
            model=value.get("model"),
        )
    if kind == "notify":
        return Notify(text=str(value.get("text", "")))
    raise EffectConfigError(f"unknown effect type {kind!r}")


def effect_to_dict(effect: Effect) -> dict[str, Any]:
    """Encode an Effect back to its `{type: value}` form."""
    if isinstance(effect, Move):
        return {"move": {
            "folder_name": effect.folder_name, "special_use": effect.special_use,
            "folder_id": str(effect.folder_id) if effect.folder_id else None,
        }}
    if isinstance(effect, Trash):
        return {"trash": {}}
    if isinstance(effect, Expunge):
        return {"expunge": {}}
    if isinstance(effect, SetFlags):
        return {"set_flags": {
            "seen": effect.seen, "flagged": effect.flagged,
            "answered": effect.answered, "deleted": effect.deleted,
        }}
    if isinstance(effect, Keywords):
        return {"keywords": {"add": list(effect.add), "remove": list(effect.remove)}}
    if isinstance(effect, Tag):
        return {"tag": {"add": list(effect.add), "remove": list(effect.remove)}}
    if isinstance(effect, RecordVerdict):
        return {"record_verdict": {
            "is_spam": effect.is_spam, "reasoning": effect.reasoning, "model": effect.model,
        }}
    if isinstance(effect, Notify):
        return {"notify": {"text": effect.text}}
    raise EffectConfigError(f"unknown effect {effect!r}")  # pragma: no cover
