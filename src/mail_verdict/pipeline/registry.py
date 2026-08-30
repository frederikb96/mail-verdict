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
        return stage_cls(definition.stage_id, config)  # type: ignore[no-any-return]
    except StageMisconfigured:
        raise
    except Exception as exc:
        # Covers both a config shape pydantic rejects and a construction
        # failure a type's own __init__ raises on otherwise-well-typed
        # config -- MatchStage parsing an unknown effect kind, most
        # notably (pipeline/effect_codec.EffectConfigError). Both are
        # "a person has to fix this" problems, never retried; folding
        # them into one exception type here is what makes that true
        # regardless of which stage of construction caught it.
        raise StageMisconfigured(
            f"stage {definition.stage_id!r}: config does not match "
            f"{definition.type!r} schema: {exc}",
            stage_id=definition.stage_id,
        ) from exc
