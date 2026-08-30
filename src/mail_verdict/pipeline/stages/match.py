"""
The `match` stage: today's rule, generalised. A condition tree evaluated
against the message and its current verdict; on a match, the configured
effects are returned for the runner to apply.

Carries `rules/conditions.py`'s condition tree unchanged, plus
`verdict_is` -- see that module for the full set of condition types.
"""

from __future__ import annotations

import builtins
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from mail_verdict.pipeline.context import RunContext
from mail_verdict.pipeline.contracts import StageOutcome
from mail_verdict.pipeline.effect_codec import parse_effect
from mail_verdict.pipeline.message_view import MessageView
from mail_verdict.rules.conditions import MailContext, evaluate_condition


class MatchConfig(BaseModel):
    """Configuration for a `match` stage."""

    when: dict[str, Any] = Field(default_factory=dict)
    effects: list[dict[str, Any]] = Field(default_factory=list)


class MatchStage:
    """A condition tree with effects to apply when it matches."""

    type: ClassVar[str] = "match"
    runs_on: ClassVar[frozenset[str]] = frozenset({"live", "historical"})

    @classmethod
    def config_schema(cls) -> builtins.type[BaseModel]:
        return MatchConfig

    def __init__(self, stage_id: str, config: MatchConfig) -> None:
        self._stage_id = stage_id
        self._config = config
        # Parsed once at construction, not per execute() -- a stage is
        # rebuilt whenever the pipeline definition changes (see
        # pipeline/revisions.py), so this still tracks edits.
        self._effects = tuple(parse_effect(e) for e in config.effects)

    async def execute(self, msg: MessageView, ctx: RunContext) -> StageOutcome:
        mail_ctx = _to_mail_context(msg, ctx)
        matched = evaluate_condition(self._config.when, mail_ctx) if self._config.when else True
        if not matched:
            return StageOutcome(matched=False, detail="conditions did not match")
        return StageOutcome(
            matched=True, effects=self._effects,
            detail=f"matched, {len(self._effects)} effect(s)",
        )


def _to_mail_context(msg: MessageView, ctx: RunContext) -> MailContext:
    return MailContext(
        mail_id=msg.message_id,
        subject=msg.subject,
        body_text=msg.body,
        body_html="",
        from_addr=msg.from_addr,
        to_addrs=list(msg.to_addrs),
        cc_addrs=list(msg.cc_addrs),
        raw_headers=dict(msg.headers),
        size_bytes=msg.size_bytes,
        has_attachments=msg.has_attachments,
        attachment_types=list(msg.attachment_types),
        folder=msg.folder.imap_name,
        tags=list(msg.tags),
        verdict_is_spam=ctx.verdict.is_spam if ctx.verdict is not None else None,
    )
