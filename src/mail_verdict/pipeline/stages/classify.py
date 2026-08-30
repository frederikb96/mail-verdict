"""
The `classify` stage: one model call, JSON in, JSON out, deciding spam or
not-spam and nothing else.

Deliberately narrow: it writes a RecordVerdict effect and never a Move --
"spam is just another stage" is only true if the classifier is a peer of
the other stages rather than one with a secret side effect. Moving a
message that was just classified spam into junk is an ordinary `match`
stage the pipeline definition composes on top of this one (see
pipeline/revisions.py's default definition).

The model gets no tools and one call per message, so a message crafted to
manipulate the prompt can influence exactly one verdict and nothing else
-- the untrusted body is fenced in delimiters in the prompt template
regardless, but the real protection is the absence of tools.
"""

from __future__ import annotations

import builtins
import json
import logging
import re
from typing import Any, ClassVar

from pydantic import BaseModel

from mail_verdict.core.prompts import load_static_prompt, render_prompt
from mail_verdict.pipeline.context import RunContext
from mail_verdict.pipeline.contracts import RecordVerdict, StageOutcome, Usage
from mail_verdict.pipeline.message_view import MessageView, build_identity_facts

logger = logging.getLogger(__name__)

_VALID_VERDICTS = {"spam", "not-spam"}
_MAX_REASONING_LENGTH = 200
_MAX_CONTENT_LENGTH = 10_000
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")

CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": sorted(_VALID_VERDICTS)},
        "reasoning": {"type": "string", "maxLength": _MAX_REASONING_LENGTH},
    },
    "required": ["verdict", "reasoning"],
    "additionalProperties": False,
}


class ClassifyConfig(BaseModel):
    """Configuration for a `classify` stage. Empty today -- neighbour
    hints are a later addition (see the design's build plan item 11)."""


def _looks_like_one_sentence(text: str) -> bool:
    return len(_SENTENCE_END_RE.findall(text.strip())) <= 1


def _validate_shape(data: dict[str, Any]) -> None:
    """Defence in depth: the provider's schema enforcement should already
    guarantee this shape, but a response that violates it anyway is a
    validation failure to retry, not something to trim silently."""
    verdict = data.get("verdict")
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"Invalid verdict {verdict!r}, expected one of {_VALID_VERDICTS}")
    reasoning = data.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning:
        raise ValueError("Missing or empty 'reasoning' in response")
    if not _looks_like_one_sentence(reasoning):
        raise ValueError("'reasoning' must be a single sentence")


def _build_context(msg: MessageView) -> dict[str, Any]:
    """Everything cheaply available about the message, stated as facts --
    never pre-judged into a score here, that is the model's job."""
    body_excerpt = msg.body
    if msg.is_truncated:
        body_excerpt = (
            f"{body_excerpt}\n[message body was too large to fetch; "
            f"classified on envelope only]"
        )
    identity = build_identity_facts(msg)
    return {
        "new_mail": {
            "from": msg.from_addr,
            "to": ", ".join(msg.to_addrs[:5]),
            "subject": msg.subject,
            "body_excerpt": body_excerpt,
            "identity": identity,
        },
    }


def _build_user_prompt(msg: MessageView) -> str:
    context_json = json.dumps(_build_context(msg), indent=2, ensure_ascii=False)
    if len(context_json) > _MAX_CONTENT_LENGTH:
        context_json = context_json[:_MAX_CONTENT_LENGTH] + "\n... [truncated]"
    return render_prompt("spam_user.md.j2", context_json=context_json)


class ClassifyStage:
    """One structured-output model call classifying spam vs not-spam."""

    type: ClassVar[str] = "classify"
    runs_on: ClassVar[frozenset[str]] = frozenset({"live"})

    @classmethod
    def config_schema(cls) -> builtins.type[BaseModel]:
        return ClassifyConfig

    def __init__(self, stage_id: str, config: ClassifyConfig) -> None:
        self._stage_id = stage_id
        self._config = config
        self._system_prompt = load_static_prompt("spam_system.md.j2")

    async def execute(self, msg: MessageView, ctx: RunContext) -> StageOutcome:
        if not ctx.account_spam_enabled:
            return StageOutcome(matched=False, detail="spam detection disabled for this account")
        if ctx.history.has_ai_verdict:
            return StageOutcome(matched=False, detail="already classified for this message key")

        ai_settings = ctx.settings.get("ai", {})
        provider = str(ai_settings.get("provider", "openai")).lower()

        if provider == "fake":
            return self._fake_verdict(msg)

        model = str(ai_settings.get("model", ""))
        effort = ai_settings.get("reasoning_effort") or None
        max_tokens = int(ai_settings.get("max_tokens", 1024))
        user_prompt = _build_user_prompt(msg)

        data, latency_ms = await ctx.models.structured_call(
            provider=provider, model=model, effort=effort, max_tokens=max_tokens,
            schema_name="spam_verdict", system_prompt=self._system_prompt,
            user_prompt=user_prompt, schema=CLASSIFY_SCHEMA, validate=_validate_shape,
        )

        is_spam = data["verdict"] == "spam"
        return StageOutcome(
            matched=True,
            effects=(RecordVerdict(is_spam=is_spam, reasoning=data["reasoning"], model=model),),
            detail=f"classified {'spam' if is_spam else 'not-spam'}",
            facts={"verdict_is_spam": is_spam},
            usage=Usage(model=model, latency_ms=latency_ms),
        )

    def _fake_verdict(self, msg: MessageView) -> StageOutcome:
        """Deterministic, keyword-driven verdict -- the test workhorse and
        the "fake" provider option for API-key-free local development."""
        keywords = ("viagra", "lottery winner", "wire transfer", "nigerian prince")
        haystack = f"{msg.subject} {msg.body}".lower()
        matched_kw = next((kw for kw in keywords if kw in haystack), None)
        is_spam = matched_kw is not None
        reasoning = (
            f"Matched configured keyword '{matched_kw}'."
            if matched_kw else "No configured keyword matched."
        )
        return StageOutcome(
            matched=True,
            effects=(RecordVerdict(is_spam=is_spam, reasoning=reasoning, model="fake"),),
            detail=f"classified {'spam' if is_spam else 'not-spam'} (fake provider)",
            facts={"verdict_is_spam": is_spam},
        )
