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

Neighbour hints (settings.semantic.neighbor_hints_enabled, default off)
add a `similar_past_mail` list to the prompt's context: the k nearest
messages carrying a human label, from pipeline/neighbors.py's
NeighborService -- never the classifier's own past verdicts, which is
what that module's docstring explains at length. Off by default so the
effect on accuracy can be measured before it is ever the default.
"""

from __future__ import annotations

import builtins
import json
import logging
import re
from typing import Any, ClassVar

from pydantic import BaseModel

from mail_verdict.core.prompts import load_static_prompt, render_prompt
from mail_verdict.embeddings.provider import DEFAULT_EMBEDDING_MODEL
from mail_verdict.pipeline.context import RunContext
from mail_verdict.pipeline.contracts import RecordVerdict, StageOutcome, Usage
from mail_verdict.pipeline.message_view import MessageView, build_identity_facts
from mail_verdict.pipeline.neighbors import NeighborHint

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
    """Configuration for a `classify` stage. Empty -- neighbour hints are
    a settings.semantic toggle (see the module docstring), not per-stage
    config, so their effect can be measured independently of any one
    pipeline definition."""


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


def _build_context(msg: MessageView, neighbor_hints: tuple[NeighborHint, ...]) -> dict[str, Any]:
    """Everything cheaply available about the message, stated as facts --
    never pre-judged into a score here, that is the model's job."""
    body_excerpt = msg.body
    if msg.is_truncated:
        body_excerpt = (
            f"{body_excerpt}\n[message body was too large to fetch; "
            f"classified on envelope only]"
        )
    identity = build_identity_facts(msg)
    context: dict[str, Any] = {
        "new_mail": {
            "from": msg.from_addr,
            "to": ", ".join(msg.to_addrs[:5]),
            "subject": msg.subject,
            "body_excerpt": body_excerpt,
            "identity": identity,
        },
    }
    if neighbor_hints:
        context["similar_past_mail"] = [
            {
                "from": hint.from_addr,
                "subject": hint.subject,
                "similarity": round(hint.similarity, 3),
                "verdict": "spam" if hint.is_spam else "not-spam",
                "evidence": _EVIDENCE_LABEL[hint.label_source],
            }
            for hint in neighbor_hints
        ]
    return context


# What each label_source means, spelled out for the model rather than left
# implicit in a bare string -- especially the asymmetry between the two
# folder-based ones, which is the whole reason they are not one label.
_EVIDENCE_LABEL = {
    "user_correction": "a person explicitly corrected this message's verdict",
    "junk_folder": "a person filed this message in Junk -- strong evidence of spam",
    "inbox_folder": (
        "this message currently sits in the inbox -- weak evidence of not-spam, "
        "since it may simply not have been dealt with yet"
    ),
}


def _build_user_prompt(msg: MessageView, neighbor_hints: tuple[NeighborHint, ...]) -> str:
    context_json = json.dumps(_build_context(msg, neighbor_hints), indent=2, ensure_ascii=False)
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

        neighbor_hints = await self._neighbor_hints(msg, ctx)
        user_prompt = _build_user_prompt(msg, neighbor_hints)

        data, latency_ms = await ctx.models.structured_call(
            provider=provider, model=model, effort=effort, max_tokens=max_tokens,
            schema_name="spam_verdict", system_prompt=self._system_prompt,
            user_prompt=user_prompt, schema=CLASSIFY_SCHEMA, validate=_validate_shape,
        )

        is_spam = data["verdict"] == "spam"
        detail = f"classified {'spam' if is_spam else 'not-spam'}"
        if neighbor_hints:
            detail += f" ({len(neighbor_hints)} neighbour hint(s))"
        return StageOutcome(
            matched=True,
            effects=(RecordVerdict(is_spam=is_spam, reasoning=data["reasoning"], model=model),),
            detail=detail,
            facts={"verdict_is_spam": is_spam, "neighbor_hint_count": len(neighbor_hints)},
            usage=Usage(model=model, latency_ms=latency_ms),
        )

    async def _neighbor_hints(
        self, msg: MessageView, ctx: RunContext,
    ) -> tuple[NeighborHint, ...]:
        """Fetch neighbour hints if settings.semantic.neighbor_hints_enabled
        is on; empty otherwise, including when the message has no
        embedding yet (NeighborService.hints_for returns nothing rather
        than raising -- see that method's docstring)."""
        semantic_settings = ctx.settings.get("semantic", {})
        if not bool(semantic_settings.get("neighbor_hints_enabled", False)):
            return ()
        embedding_model = str(semantic_settings.get("model", DEFAULT_EMBEDDING_MODEL))
        k = int(semantic_settings.get("neighbor_k", 5))
        min_similarity = float(semantic_settings.get("neighbor_min_similarity", 0.75))
        hints = await ctx.neighbors.hints_for(
            msg_key=msg.msg_key, model=embedding_model, k=k, min_similarity=min_similarity,
        )
        return tuple(hints)

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
