"""
The "Add rule" assistant: one sentence from the owner about the mail he has
open becomes one proposed change to his rules, which he accepts or
declines. Nothing is stored -- Accept is the client replacing the pipeline
document with the proposed one, carrying the `base_revision` the proposal
was computed against.

A rule is a `match` stage of the pipeline document. The model sees the
whole stage list as JSON text and answers with find-and-replace edits over
that text: each names a piece of the current text, which must occur exactly
once, and what replaces it -- nothing, to delete. So one proposal can add,
change, remove and reorder any number of rules at once, and the edit format
stays as simple as quoting what is there. The pipeline's other stages
appear in the text in a shortened form and must come back unchanged.

The exchange is fixed rather than a free tool loop. Left to itself a model
never looks at other mail, so step one forces the question "which searches
do you want?", the searches run, and step two answers with the edits. A
candidate that fails validation goes back to the model with the reason, up
to `_MAX_ATTEMPTS` times; the person only ever sees one that passed.

Validation is everything the write endpoints would check plus what only
this feature needs: the change must concern the mail that prompted it, must
not introduce an effect the assistant may not propose, and no rule it adds
or changes may catch an implausible share of the account's newest mail.
That control set also feeds the preview shown beside the proposal.

The mail content in the model's input is untrusted. The model has no tools
and one answer per call, the input is fenced and escaped like the spam
classifier's (core/prompts.py), and the worst a crafted message can do is
influence one proposal the person still has to read and accept.
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import Select, func, select

from mail_verdict.api.schemas import (
    RuleAssistantChange,
    RuleAssistantPreview,
    RuleAssistantPreviewExample,
    RuleAssistantResponse,
    RuleAssistantRuleChange,
    StageOut,
)
from mail_verdict.core.prompts import escape_delimiter_breakout, render_prompt
from mail_verdict.core.retry import RetryConfig
from mail_verdict.database.models import Folder, Message
from mail_verdict.database.repository import AccountRepository
from mail_verdict.pipeline import health as pipeline_health
from mail_verdict.pipeline.context import (
    ModelGateway,
    VerdictView,
    current_verdict_for_mail,
)
from mail_verdict.pipeline.document_validation import DocumentValidationError, validate_document
from mail_verdict.pipeline.effect_codec import render_effect_syntax
from mail_verdict.pipeline.message_view import (
    MessageView,
    extract_display_name_and_addr,
    load_message_view,
)
from mail_verdict.pipeline.revisions import (
    PipelineDefinition,
    PipelineRevisionRepository,
    definition_to_document,
)
from mail_verdict.pipeline.stages.match import matches_message
from mail_verdict.rules.conditions import render_condition_syntax

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.settings.credentials import ProviderCredentialRepository
    from mail_verdict.settings.service import SettingsService

logger = logging.getLogger(__name__)

# How many of the account's newest mails a proposal is measured against.
_CONTROL_SIZE = 100
_MAX_SEARCHES = 3
_MAX_ATTEMPTS = 3
# The settings.ai budget is sized for a one-word verdict; edits quoting and
# re-emitting several rules need more.
_MAX_TOKENS = 8000
_CALL_TIMEOUT_SECONDS = 60.0
_BODY_EXCERPT_CHARS = 1200
_SEARCH_SAMPLE_SIZE = 8
_SEARCH_TERM_CHARS = 100
_SUBJECT_SAMPLE_CHARS = 80
_PREVIEW_EXAMPLES = 5

# Effects the assistant may never introduce, because the model reads
# untrusted mail text and a careless Accept of any of these is not undoable
# (expunge, webhook) or is not what a person writing a rule means
# (the pipeline's own bookkeeping effects). A rule that already carries one
# can still be changed or removed -- nothing is introduced -- and the
# proposal then warns (_warnings).
ASSISTANT_DENIED_EFFECTS = frozenset(
    {"record_verdict", "enqueue_order", "notify", "expunge", "webhook"}
)

# The keys a match stage may carry; anything else is a model invention.
_STAGE_KEYS = frozenset({"stage_id", "type", "name", "config", "enabled", "halt", "accounts"})

SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["searches"],
    "properties": {
        "searches": {
            "type": "array",
            "maxItems": _MAX_SEARCHES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["from_contains", "subject_contains"],
                "properties": {
                    "from_contains": {"type": "string"},
                    "subject_contains": {"type": "string"},
                },
            },
        },
    },
}

CHANGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["edits", "message"],
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["old", "new"],
                "properties": {
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
            },
        },
        "message": {"type": "string"},
    },
}


class AssistantMessageNotFound(LookupError):
    """The mail the owner has open does not exist as a mirrored message --
    gone, or a glacier row, which the assistant does not cover."""


class _Rejected(ValueError):
    """A candidate that failed validation; the text goes back to the model."""


class _Disconnected(Exception):
    """The caller went away; the response will never be read."""


@dataclass(frozen=True)
class _RuleChange:
    """One rule the proposal touches. `before` is None for an added rule,
    `after` None for a removed one; a moved rule has both, identical."""

    kind: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None

    @property
    def stage_id(self) -> str:
        return cast("str", (self.after or self.before or {})["stage_id"])

    @property
    def name(self) -> str:
        return cast("str", (self.after or self.before or {})["name"])


@dataclass(frozen=True)
class _Candidate:
    """A validated change, ready to show: the complete new stage list and
    the rules in it that differ from the current one."""

    stages: list[dict[str, Any]]
    changes: list[_RuleChange]
    message: str


@dataclass
class _Scene:
    """Everything one request reads from the database, loaded once."""

    view: MessageView
    verdict: VerdictView | None
    definition: PipelineDefinition
    stages: list[dict[str, Any]]
    accounts: list[dict[str, str]]
    folder_names: dict[uuid.UUID, str]
    control: list[MessageView]


# --- Statements ----------------------------------------------------------
#
# Both name their columns: `messages` carries bodies and raw source, which
# must never ride along into a sample sent to a model provider.


def control_statement(account_id: uuid.UUID) -> Select[Any]:
    """Ids of the account's newest non-draft, non-expunged messages."""
    return (
        select(Message.id)
        .where(
            Message.account_id == account_id, Message.expunged_at.is_(None),
            Message.is_draft.is_(False),
        )
        .order_by(Message.received_at.desc().nulls_last())
        .limit(_CONTROL_SIZE)
    )


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _search_filters(
    account_id: uuid.UUID, from_contains: str, subject_contains: str,
) -> list[Any]:
    filters: list[Any] = [Message.account_id == account_id, Message.expunged_at.is_(None)]
    if from_contains:
        filters.append(Message.from_addr.ilike(_like(from_contains), escape="\\"))
    if subject_contains:
        filters.append(Message.subject.ilike(_like(subject_contains), escape="\\"))
    return filters


def search_sample_statement(
    account_id: uuid.UUID, from_contains: str, subject_contains: str,
) -> Select[Any]:
    """Newest mails whose sender and/or subject contain the given text."""
    return (
        select(Message.from_addr, Message.subject, Message.folder_id)
        .where(*_search_filters(account_id, from_contains, subject_contains))
        .order_by(Message.received_at.desc().nulls_last())
        .limit(_SEARCH_SAMPLE_SIZE)
    )


def search_count_statement(
    account_id: uuid.UUID, from_contains: str, subject_contains: str,
) -> Select[Any]:
    return select(func.count()).select_from(Message).where(
        *_search_filters(account_id, from_contains, subject_contains)
    )


# --- Matching --------------------------------------------------------------


def stage_matches(stage: dict[str, Any], view: MessageView, verdict: VerdictView | None) -> bool:
    """Whether a `match` stage's condition holds for a message and the
    stage applies to its account. Independent of `enabled`, and of whether
    the pipeline would still run the stage on a mail already filed away --
    the assistant is asked about mail the owner is looking at, wherever it
    sits."""
    if stage.get("type") != "match":
        return False
    accounts = stage.get("accounts")
    if accounts and str(view.account_id) not in {str(a) for a in accounts}:
        return False
    when = (stage.get("config") or {}).get("when") or {}
    try:
        return matches_message(when, view, verdict)
    except ValueError:
        return False


def _hits(stage: dict[str, Any] | None, control: list[MessageView]) -> list[bool]:
    if stage is None:
        return [False] * len(control)
    return [stage_matches(stage, view, None) for view in control]


# --- Prompt ------------------------------------------------------------------


def system_prompt(step: str) -> str:
    """The static instructions for step "search" or "change"."""
    return render_prompt(
        "rule_assistant_system.md.j2", step=step,
        condition_syntax=render_condition_syntax(),
        effect_syntax=render_effect_syntax(exclude=ASSISTANT_DENIED_EFFECTS),
    )


def user_prompt(model_input: dict[str, Any]) -> str:
    """The input object as the fenced, escaped text the model reads."""
    body = escape_delimiter_breakout(json.dumps(model_input, ensure_ascii=False))
    return f"<input>\n{body}\n</input>"


def _model_view_of_stage(stage: dict[str, Any]) -> dict[str, Any]:
    """A match stage whole; any other stage shortened to what identifies it,
    since the assistant may not change it."""
    if stage.get("type") == "match":
        return stage
    return {"stage_id": stage["stage_id"], "type": stage["type"], "name": stage.get("name")}


def rules_text(stages: list[dict[str, Any]]) -> str:
    """The stage list as the JSON text the model edits."""
    return _json_text([_model_view_of_stage(s) for s in stages])


# --- Validation of one candidate ---------------------------------------------


def _effects_of(stage: dict[str, Any] | None) -> list[Any]:
    config = (stage or {}).get("config")
    effects = config.get("effects") if isinstance(config, dict) else None
    return effects if isinstance(effects, list) else []


def _introduced_denied_kinds(before: dict[str, Any] | None, after: dict[str, Any]) -> list[str]:
    """Denied effect kinds present after the change that were not already
    present, identically, before it."""
    existing = _effects_of(before)
    kinds: set[str] = set()
    for effect in _effects_of(after):
        if isinstance(effect, dict) and effect not in existing:
            kinds |= set(effect) & ASSISTANT_DENIED_EFFECTS
    return sorted(kinds)


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _stage_out(stage: dict[str, Any]) -> StageOut:
    accounts = stage.get("accounts")
    return StageOut(
        stage_id=stage["stage_id"], type=stage["type"], name=stage["name"],
        config=stage["config"], enabled=stage["enabled"], halt=stage["halt"],
        accounts=[uuid.UUID(str(a)) for a in accounts] if accounts else None,
    )


def apply_edits(text: str, edits: Any) -> str:
    """Apply find-and-replace edits in order, each to the text the ones
    before it left. Each `old` must occur exactly once."""
    if not isinstance(edits, list):
        raise _Rejected("'edits' must be a list")
    for number, edit in enumerate(edits, start=1):
        old = edit.get("old") if isinstance(edit, dict) else None
        new = edit.get("new") if isinstance(edit, dict) else None
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise _Rejected(f"edit {number}: 'old' must be non-empty text and 'new' text")
        count = text.count(old)
        if count == 0:
            raise _Rejected(
                f"edit {number}: its 'old' text does not occur in rules_json as the edits "
                "before it left it -- copy it exactly, indentation included"
            )
        if count > 1:
            raise _Rejected(
                f"edit {number}: its 'old' text occurs {count} times -- include more of the "
                "surrounding text, such as the rule's stage_id line, so it occurs once"
            )
        text = text.replace(old, new, 1)
    return text


def _parse_stages(scene: _Scene, text: str) -> list[dict[str, Any]]:
    """The edited text back as a complete stage list: other stages restored
    from their shortened form, match stages checked for shape unless they
    came back exactly as they were."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _Rejected(f"after the edits, rules_json is not valid JSON: {exc}") from None
    if not isinstance(parsed, list) or not all(isinstance(s, dict) for s in parsed):
        raise _Rejected("after the edits, rules_json must still be a list of stage objects")

    current = {s["stage_id"]: s for s in scene.stages}
    others = [s["stage_id"] for s in scene.stages if s["type"] != "match"]
    stages: list[dict[str, Any]] = []
    kept_others: list[Any] = []
    for raw in parsed:
        stage_id = raw.get("stage_id")
        original = current.get(stage_id) if isinstance(stage_id, str) else None
        if raw.get("type") != "match" or (original is not None and original["type"] != "match"):
            if original is None or raw != _model_view_of_stage(original):
                raise _Rejected(
                    f"only match rules may be changed; stage {stage_id!r} must stay exactly "
                    "as it was in rules_json"
                )
            kept_others.append(stage_id)
            stages.append(copy.deepcopy(original))
        elif original is not None and raw == original:
            stages.append(copy.deepcopy(original))
        else:
            stages.append(_checked_rule(scene, raw))
    if kept_others != others:
        raise _Rejected(f"the stages {others} must all stay, in this order")
    ids = [s["stage_id"] for s in stages]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise _Rejected(f"stage_id {duplicates[0]!r} is used twice; every rule needs its own")
    return stages


def _checked_rule(scene: _Scene, raw: dict[str, Any]) -> dict[str, Any]:
    """A new or changed match stage, its shape checked field by field."""
    stage_id = raw.get("stage_id")
    if not isinstance(stage_id, str) or not stage_id:
        raise _Rejected("every rule needs a non-empty 'stage_id'")
    unknown = sorted(set(raw) - _STAGE_KEYS)
    if unknown:
        raise _Rejected(f"rule {stage_id!r}: unknown keys {unknown}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _Rejected(f"rule {stage_id!r} needs a non-empty 'name'")
    for flag in ("enabled", "halt"):
        if not isinstance(raw.get(flag), bool):
            raise _Rejected(f"rule {stage_id!r}: {flag!r} must be true or false")
    known = {a["id"] for a in scene.accounts}
    accounts = raw.get("accounts", "missing")
    if accounts is not None and (
        not isinstance(accounts, list) or not accounts
        or not all(str(a) in known for a in accounts)
    ):
        raise _Rejected(
            f"rule {stage_id!r}: 'accounts' must be null (every account) or a non-empty list "
            f"of account ids from {sorted(known)}"
        )
    config = raw.get("config")
    try:
        _require_condition_and_effects(config)
    except _Rejected as exc:
        raise _Rejected(f"rule {stage_id!r}: {exc}") from None
    return {
        "stage_id": stage_id, "type": "match", "name": name.strip(), "config": config,
        "enabled": raw["enabled"], "halt": raw["halt"],
        "accounts": [str(a) for a in accounts] if accounts else None,
    }


def _rule_changes(
    before: list[dict[str, Any]], after: list[dict[str, Any]],
) -> list[_RuleChange]:
    """Every rule that differs, in the new order, removed ones last. A rule
    is moved when its content is unchanged but its place among the rules
    both lists share is not."""
    old = {s["stage_id"]: s for s in before}
    new = {s["stage_id"]: s for s in after}
    shared_before = [i for i in old if i in new]
    shared_after = [i for i in new if i in old]
    changes: list[_RuleChange] = []
    for stage in after:
        previous = old.get(stage["stage_id"])
        if previous is None:
            changes.append(_RuleChange("added", None, stage))
        elif previous != stage:
            changes.append(_RuleChange("changed", previous, stage))
        elif (
            stage["type"] == "match"
            and shared_before.index(stage["stage_id"]) != shared_after.index(stage["stage_id"])
        ):
            changes.append(_RuleChange("moved", previous, stage))
    changes.extend(_RuleChange("removed", s, None) for s in before if s["stage_id"] not in new)
    return changes


def _require_condition_and_effects(config: Any) -> None:
    if not isinstance(config, dict):
        raise _Rejected("'config' must be an object with 'when' and 'effects'")
    if not config.get("when"):
        raise _Rejected("'config.when' is empty, which would match every mail")
    if not isinstance(config.get("effects"), list) or not config["effects"]:
        raise _Rejected("'config.effects' must be a non-empty list")


async def _validate(
    db: DatabaseConnection, scene: _Scene, data: dict[str, Any],
) -> _Candidate:
    """Turn the model's edits into a checked candidate or raise `_Rejected`
    with the reason, in the order the model can most easily act on."""
    stages = _parse_stages(scene, apply_edits(rules_text(scene.stages), data.get("edits")))
    changes = _rule_changes(scene.stages, stages)
    if not changes:
        raise _Rejected("the edits change nothing; answer with no edits if nothing should change")
    written = [c.after for c in changes if c.after is not None and c.kind != "moved"]

    for stage in written:
        denied = _introduced_denied_kinds(
            next((c.before for c in changes if c.after is stage), None), stage,
        )
        if denied:
            raise _Rejected(
                f"rule {stage['stage_id']!r}: the effect(s) {denied} may not be introduced by "
                "this assistant; use only the effect types listed"
            )

    try:
        definitions = validate_document({"enabled": scene.definition.enabled, "stages": stages})
    except DocumentValidationError as exc:
        raise _Rejected("; ".join(exc.problems)) from None

    written_ids = {s["stage_id"] for s in written}
    unresolved = [
        e for e in await pipeline_health.compute_health(
            db, [d for d in definitions if d.stage_id in written_ids],
            account_ids=[uuid.UUID(a["id"]) for a in scene.accounts],
        ) if not e.ok
    ]
    if unresolved:
        entry = unresolved[0]
        names = sorted(scene.folder_names.values())
        raise _Rejected(
            f"rule {entry.stage_id!r}: folder {entry.reference!r} does not exist in account "
            f"{entry.account_id}; folders of the open mail's account: {names}"
        )

    if not any(
        stage is not None and stage_matches(stage, scene.view, scene.verdict)
        for change in changes for stage in (change.before, change.after)
    ):
        raise _Rejected(
            "none of the rules the edits add, change, move or remove match the open mail, "
            "before or after the change"
        )

    for change in changes:
        if change.after is None or change.kind == "moved":
            continue
        caught = sum(_hits(change.after, scene.control))
        newly = caught - sum(_hits(change.before, scene.control))
        if newly > _CONTROL_SIZE // 2:
            raise _Rejected(
                f"rule {change.stage_id!r} matches {caught} of the last {len(scene.control)} "
                "mails -- far too broad"
            )

    return _Candidate(
        stages=stages, changes=changes, message=str(data.get("message") or "").strip(),
    )


def _title(changes: list[_RuleChange]) -> str:
    if len(changes) == 1:
        change = changes[0]
        verb = {
            "added": "New rule", "changed": "Change rule", "moved": "Move rule",
            "removed": "Remove rule",
        }[change.kind]
        return f'{verb} "{change.name}"'
    counts = [
        f"{n} {kind}" for kind in ("added", "changed", "moved", "removed")
        if (n := sum(c.kind == kind for c in changes))
    ]
    return f"Change {len(changes)} rules: {', '.join(counts)}"


def _caught_by_any(stages: list[dict[str, Any]], control: list[MessageView]) -> list[bool]:
    return [any(stage_matches(stage, view, None) for stage in stages) for view in control]


def _preview(scene: _Scene, candidate: _Candidate) -> RuleAssistantPreview | None:
    """What the touched rules catch among the newest mails, together,
    before and after."""
    if not scene.control:
        return None
    before_hits = _caught_by_any(
        [c.before for c in candidate.changes if c.before is not None], scene.control,
    )
    after_hits = _caught_by_any(
        [c.after for c in candidate.changes if c.after is not None], scene.control,
    )
    examples = [
        RuleAssistantPreviewExample(from_addr=view.from_addr, subject=view.subject)
        for view, was, now in zip(scene.control, before_hits, after_hits, strict=True)
        if now and not was
    ][:_PREVIEW_EXAMPLES]
    return RuleAssistantPreview(
        sample_size=len(scene.control), matched_before=sum(before_hits),
        matched_after=sum(after_hits), examples=examples,
    )


def _denied_kinds_of(stage: dict[str, Any] | None) -> list[str]:
    return sorted(
        {
            kind for effect in _effects_of(stage) if isinstance(effect, dict)
            for kind in set(effect) & ASSISTANT_DENIED_EFFECTS
        }
    )


def _warnings(scene: _Scene, candidate: _Candidate) -> list[str]:
    """Never errors. A changed rule that already carries a denied effect (a
    webhook, an expunge) can make that effect act on more mail; a removed
    one stops doing it; and a rule that catches the open mail is never
    reached for it when an earlier rule stops the pipeline first."""
    warnings: list[str] = []
    for change in candidate.changes:
        kinds = _denied_kinds_of(change.before)
        if not kinds or change.kind == "moved":
            continue
        if change.kind == "removed":
            warnings.append(f'Rule "{change.name}" carries {", ".join(kinds)} and is removed.')
        else:
            warnings.append(
                f'Rule "{change.name}" carries {", ".join(kinds)}, so this change can make it '
                "act on mail it does not act on now."
            )

    touched = {c.stage_id for c in candidate.changes if c.after is not None}
    stoppers: list[dict[str, Any]] = []
    for stage in candidate.stages:
        if not (stage.get("enabled", True) and stage_matches(stage, scene.view, scene.verdict)):
            continue
        if stage["stage_id"] in touched and stoppers:
            warnings.append(
                f'Rule "{stoppers[0]["name"]}" already stops this mail earlier in the list, '
                f'so "{stage["name"]}" would not be reached for it.'
            )
        if stage.get("halt"):
            stoppers.append(stage)
    return warnings


# --- The exchange --------------------------------------------------------------


class _Model:
    """The configured model, called at most `_MAX_ATTEMPTS + 1` times."""

    def __init__(
        self, db: DatabaseConnection, settings_service: SettingsService,
        cred_repo: ProviderCredentialRepository,
        is_disconnected: Callable[[], Awaitable[bool]],
    ) -> None:
        ai = settings_service.get("ai")
        self.provider = str(ai.get("provider", "openai")).lower()
        self.name = "fake" if self.provider == "fake" else str(ai.get("model", ""))
        self._effort = ai.get("reasoning_effort") or None
        self._base_url = ai.get("base_url") or None
        self._gateway = ModelGateway(
            db, cred_repo, RetryConfig.from_settings(settings_service.get("retry")),
        )
        self._is_disconnected = is_disconnected
        self.calls = 0

    async def ask(
        self, *, step: str, schema_name: str, schema: dict[str, Any], model_input: dict[str, Any],
    ) -> dict[str, Any]:
        if await self._is_disconnected():
            raise _Disconnected
        self.calls += 1
        data, _ = await self._gateway.structured_call(
            provider=self.provider, category="assistant", model=self.name, effort=self._effort,
            max_tokens=_MAX_TOKENS, schema_name=schema_name, system_prompt=system_prompt(step),
            user_prompt=user_prompt(model_input), schema=schema, base_url=self._base_url,
            timeout_seconds=_CALL_TIMEOUT_SECONDS,
        )
        return data


async def _load_scene(
    db: DatabaseConnection, message_id: uuid.UUID,
) -> _Scene:
    async with db.session() as session:
        view = await load_message_view(session, message_id)
        if view is None:
            raise AssistantMessageNotFound(str(message_id))
        folder_rows = (
            await session.execute(
                select(Folder.id, Folder.imap_name).where(
                    Folder.account_id == view.account_id, Folder.deleted_at.is_(None),
                )
            )
        ).all()
        control = await _load_control(session, view.account_id)

    verdict = await current_verdict_for_mail(db, message_id)
    definition = await PipelineRevisionRepository(db).current() or PipelineDefinition(
        revision=0, enabled=True, stages=(),
    )
    stages = cast(
        "list[dict[str, Any]]", copy.deepcopy(definition_to_document(definition)["stages"]),
    )
    accounts = [
        {"id": str(a.id), "name": a.name} for a in await AccountRepository(db).get_all()
    ]
    return _Scene(
        view=view, verdict=verdict, definition=definition, stages=stages,
        accounts=accounts, folder_names={row.id: row.imap_name for row in folder_rows},
        control=control,
    )


async def _load_control(session: AsyncSession, account_id: uuid.UUID) -> list[MessageView]:
    ids = (await session.execute(control_statement(account_id))).scalars().all()
    views = [await load_message_view(session, mail_id) for mail_id in ids]
    return [v for v in views if v is not None]


async def _search(
    db: DatabaseConnection, scene: _Scene, from_contains: str, subject_contains: str,
) -> dict[str, Any]:
    if not from_contains and not subject_contains:
        return {"from_contains": "", "subject_contains": "", "total": 0, "sample": []}
    account_id = scene.view.account_id
    async with db.session() as session:
        total = (
            await session.execute(
                search_count_statement(account_id, from_contains, subject_contains)
            )
        ).scalar_one()
        rows = (
            await session.execute(
                search_sample_statement(account_id, from_contains, subject_contains)
            )
        ).all()
    return {
        "from_contains": from_contains, "subject_contains": subject_contains, "total": total,
        "sample": [
            {
                "from": row.from_addr, "subject": (row.subject or "")[:_SUBJECT_SAMPLE_CHARS],
                "folder": scene.folder_names.get(row.folder_id, "?"),
            }
            for row in rows
        ],
    }


def _search_term(raw: Any) -> str:
    return raw.strip()[:_SEARCH_TERM_CHARS] if isinstance(raw, str) else ""


def _fake_answer(scene: _Scene) -> dict[str, Any]:
    """What the `fake` provider answers: a rule flagging this sender. No
    model is called, so tests and a local stack exercise everything but the
    model itself -- validation, preview, and the write Accept implies."""
    _, bare = extract_display_name_and_addr(scene.view.from_addr)
    taken = {s["stage_id"] for s in scene.stages}
    base = f"assistant-{str(scene.view.message_id)[:8]}"
    stage_id = base
    suffix = 2
    while stage_id in taken:
        stage_id, suffix = f"{base}-{suffix}", suffix + 1
    stage = {
        "stage_id": stage_id, "type": "match", "name": f"Mail from {bare}",
        "config": {
            "when": {"sender_match": bare}, "effects": [{"set_flags": {"flagged": True}}],
        },
        "enabled": True, "halt": False, "accounts": [str(scene.view.account_id)],
    }
    rendered = _json_text([stage])[1:-1].strip("\n")
    edit = (
        {"old": "[]", "new": f"[\n{rendered}\n]"} if not scene.stages
        else {"old": "\n]", "new": f",\n{rendered}\n]"}
    )
    return {"edits": [edit], "message": f"Mail from {bare} will be flagged."}


def _model_input(
    scene: _Scene, request: str, matched_today: list[str], same_sender: dict[str, Any],
) -> dict[str, Any]:
    view = scene.view
    list_id = view.headers.get("list-id")
    mail: dict[str, Any] = {
        "account_id": str(view.account_id), "folder": view.folder.imap_name,
        "from": view.from_addr, "to": list(view.to_addrs), "subject": view.subject,
    }
    if list_id:
        mail["list_id"] = list_id
    mail["body_excerpt"] = view.body[:_BODY_EXCERPT_CHARS]
    return {
        "request": request, "mail": mail, "matched_today": matched_today,
        "same_sender_recent": {
            "total": same_sender["total"],
            "sample": [
                {"subject": m["subject"], "folder": m["folder"]} for m in same_sender["sample"]
            ],
        },
        "accounts": scene.accounts, "folders": sorted(scene.folder_names.values()),
        "rules_json": rules_text(scene.stages), "searches": [],
    }


async def propose(
    *,
    db: DatabaseConnection,
    settings_service: SettingsService,
    cred_repo: ProviderCredentialRepository,
    message_id: uuid.UUID,
    prompt: str,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> RuleAssistantResponse | None:
    """
    Run the exchange for one open mail and one sentence.

    Args:
        db: Database connection
        settings_service: Source of the `ai` model settings
        cred_repo: Provider credentials
        message_id: The mail the owner has open
        prompt: The owner's sentence, already stripped
        is_disconnected: Polled before every model call; True ends the
            work quietly, since nobody will read the answer

    Returns:
        The proposal (`change` null when there is nothing to accept), or
        None when the caller disconnected

    Raises:
        AssistantMessageNotFound: no such mirrored message
        StageUnavailable | StageThrottled | StageTransient: the model call failed
    """
    scene = await _load_scene(db, message_id)
    matched_today = [
        s["stage_id"] for s in scene.stages
        if s.get("enabled", True) and stage_matches(s, scene.view, scene.verdict)
    ]
    model = _Model(db, settings_service, cred_repo, is_disconnected)
    _, bare_sender = extract_display_name_and_addr(scene.view.from_addr)
    model_input = _model_input(
        scene, prompt, matched_today, await _search(db, scene, bare_sender, ""),
    )
    revision = scene.definition.revision

    def respond(
        message: str, candidate: _Candidate | None = None,
    ) -> RuleAssistantResponse:
        if candidate is None:
            return RuleAssistantResponse(
                message=message, model=model.name, model_calls=model.calls,
            )
        change = RuleAssistantChange(
            base_revision=revision, enabled=scene.definition.enabled,
            stages=[_stage_out(s) for s in candidate.stages],
            title=_title(candidate.changes),
            rules=[
                RuleAssistantRuleChange(
                    kind=c.kind,  # type: ignore[arg-type]
                    stage_id=c.stage_id, name=c.name,
                    before_text=(
                        _json_text(c.before) if c.before is not None and c.kind != "moved"
                        else None
                    ),
                    after_text=_json_text(c.after) if c.after is not None else None,
                )
                for c in candidate.changes
            ],
        )
        return RuleAssistantResponse(
            message=message or change.title, change=change,
            preview=_preview(scene, candidate), warnings=_warnings(scene, candidate),
            model=model.name, model_calls=model.calls,
        )

    try:
        if model.provider != "fake":
            plan = await model.ask(
                step="search", schema_name="rule_searches", schema=SEARCH_SCHEMA,
                model_input=model_input,
            )
            searches = plan.get("searches")
            for entry in (searches if isinstance(searches, list) else [])[:_MAX_SEARCHES]:
                if not isinstance(entry, dict):
                    continue
                from_contains = _search_term(entry.get("from_contains"))
                subject_contains = _search_term(entry.get("subject_contains"))
                if from_contains or subject_contains:
                    model_input["searches"].append(
                        await _search(db, scene, from_contains, subject_contains)
                    )

        last_error = ""
        answer: dict[str, Any] | None = None
        for _ in range(_MAX_ATTEMPTS):
            if model.provider == "fake":
                answer = _fake_answer(scene)
            else:
                answer = await model.ask(
                    step="change", schema_name="rule_change", schema=CHANGE_SCHEMA,
                    model_input=model_input,
                )
            if answer.get("edits") == []:
                return respond(str(answer.get("message") or "").strip() or "No change needed.")
            try:
                candidate = await _validate(db, scene, answer)
            except _Rejected as exc:
                last_error = str(exc)
                logger.info("Rule assistant candidate rejected", extra={"reason": last_error})
                model_input["previous_answer"] = answer
                model_input["error"] = last_error
                continue
            return respond(candidate.message, candidate)
    except _Disconnected:
        return None

    return respond(
        f"I could not turn that into a valid rule change ({last_error}). "
        "Try rephrasing it, or edit the rules by hand."
    )
