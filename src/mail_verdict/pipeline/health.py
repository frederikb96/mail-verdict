"""
Per-stage folder resolution: whether a `match` stage's `move` effects
resolve against the accounts they apply to, right now.

Separate from document_validation.py's hard checks on purpose. A folder
reference that does not resolve is not invalid -- folders appear
asynchronously as PostIMAP discovers them, so a stage can legitimately be
written before its folder exists. This module makes that state queryable
after the fact (GET /api/pipeline/health) rather than only reported once,
at write time, and then forgotten.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mail_verdict.pipeline.context import FolderResolver

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.pipeline.contracts import StageDefinition


@dataclass(frozen=True)
class HealthEntry:
    """One folder reference's resolution against one account."""

    stage_id: str
    account_id: uuid.UUID
    reference: str
    ok: bool
    detail: str | None


async def compute_health(
    db: DatabaseConnection, stages: list[StageDefinition], *, account_ids: list[uuid.UUID],
) -> list[HealthEntry]:
    """
    Resolve every `move` effect's folder reference in every `match` stage,
    against every account the stage applies to.

    Args:
        db: Database connection
        stages: The pipeline definition's stages
        account_ids: Every account that exists -- used for a stage whose
            own `accounts` is None (applies to all)

    Returns:
        One entry per (stage, account, reference) combination checked;
        empty for a stage with no folder-referencing effects
    """
    entries: list[HealthEntry] = []
    for stage in stages:
        if stage.type != "match" or not isinstance(stage.config, dict):
            continue
        move_refs = [
            effect["move"]
            for effect in stage.config.get("effects", [])
            if isinstance(effect, dict) and effect.get("move")
        ]
        if not move_refs:
            continue

        target_accounts = list(stage.accounts) if stage.accounts else account_ids
        for account_id in target_accounts:
            resolver = FolderResolver(db, account_id)
            for move in move_refs:
                entries.append(await _check_one(resolver, stage.stage_id, account_id, move))
    return entries


async def _check_one(
    resolver: FolderResolver, stage_id: str, account_id: uuid.UUID, move: dict[str, Any],
) -> HealthEntry:
    folder_id_raw = move.get("folder_id")
    folder_id = uuid.UUID(folder_id_raw) if folder_id_raw else None
    reference = str(move.get("folder_name") or move.get("special_use") or folder_id or "?")

    resolved = await resolver.resolve(
        folder_name=move.get("folder_name"), special_use=move.get("special_use"),
        folder_id=folder_id,
    )
    return HealthEntry(
        stage_id=stage_id, account_id=account_id, reference=reference, ok=resolved is not None,
        detail=None if resolved is not None else f"folder {reference!r} does not resolve",
    )


__all__ = ["HealthEntry", "compute_health"]
