"""
The stage type registry: `type` string -> the class implementing it.

A stage instance is built fresh from a StageDefinition every time the
pipeline definition changes, never mutated in place -- see
pipeline/revisions.py.
"""

from __future__ import annotations

import builtins

from mail_verdict.pipeline.contracts import Stage, StageDefinition, StageMisconfigured
from mail_verdict.pipeline.stages.classify import ClassifyStage
from mail_verdict.pipeline.stages.match import MatchStage

STAGE_TYPES: dict[str, builtins.type[Stage]] = {
    MatchStage.type: MatchStage,
    ClassifyStage.type: ClassifyStage,
}


def build_stage(definition: StageDefinition) -> Stage:
    """
    Instantiate a stage from its definition.

    Raises:
        StageMisconfigured: the type is unknown, or the config does not
            validate against that type's schema -- both are things a
            person has to fix, not transient failures.
    """
    stage_cls = STAGE_TYPES.get(definition.type)
    if stage_cls is None:
        raise StageMisconfigured(
            f"stage {definition.stage_id!r}: unknown stage type {definition.type!r}",
            stage_id=definition.stage_id,
        )
    schema = stage_cls.config_schema()
    try:
        config = schema.model_validate(dict(definition.config))
    except Exception as exc:
        raise StageMisconfigured(
            f"stage {definition.stage_id!r}: config does not match "
            f"{definition.type!r} schema: {exc}",
            stage_id=definition.stage_id,
        ) from exc
    return stage_cls(definition.stage_id, config)  # type: ignore[no-any-return]
