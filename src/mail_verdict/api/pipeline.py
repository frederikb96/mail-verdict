"""
Pipeline configuration API -- the surface an agent (or the rules UI page)
edits the caller's own pipeline document through, instead of inserting into
pipeline_revisions by hand. Every user has one document; every route here
reads and writes the caller's, and a message named in a test or an
assistant request must be on one of the caller's accounts.

GET/PUT   /api/pipeline                     -- read/replace the whole document
GET       /api/pipeline/stage-types         -- registry: type, runs_on, JSON schema
POST      /api/pipeline/stages              -- add one stage
PATCH     /api/pipeline/stages/{id}         -- partially update one stage
DELETE    /api/pipeline/stages/{id}         -- remove one stage
POST      /api/pipeline/stages/reorder      -- set the full stage order
GET       /api/pipeline/revisions           -- history
POST      /api/pipeline/revisions/{n}/restore
GET       /api/pipeline/health              -- per-stage folder resolution
POST      /api/pipeline/test                -- dry-run the whole pipeline
POST      /api/pipeline/stages/{id}/test    -- dry-run one stage
POST      /api/pipeline/assistant           -- propose a change to the rules from a sentence
GET       /api/admin/pipeline/overview      -- admin: every user's document as counts

A user who has no document yet gets the starting one (pipeline/template.py)
the first time any route here reads it.

Validation is split deliberately (see pipeline/document_validation.py and
pipeline/health.py): a syntax error, an unknown stage type, an unknown
effect or condition type, and a duplicate stage name can never become
valid later, so every write endpoint rejects them with 400. A folder
reference that does not currently resolve is accepted -- folders appear
asynchronously -- and is reported instead through the warnings on every
document response and through GET /api/pipeline/health.

Every write is optimistic: a request may carry base_revision, the
revision its edit was computed against; a stale one gets 409 rather than
silently overwriting a concurrent writer (an agent and the UI editing at
once, most notably). Omitting it writes unconditionally.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select

from mail_verdict.api.deps import current_scope, require_admin
from mail_verdict.api.events import get_event_ring
from mail_verdict.api.schemas import (
    PipelineDocumentOut,
    PipelineHealthEntryOut,
    PipelineRevisionSummary,
    PipelineTestRequest,
    PipelineTestResponse,
    PipelineWriteRequest,
    RuleAssistantRequest,
    RuleAssistantResponse,
    StageCreateRequest,
    StageOut,
    StageReorderRequest,
    StageTypeOut,
    StageUpdateRequest,
)
from mail_verdict.auth.scope import Scope
from mail_verdict.database.connection import DatabaseConnection, get_db_connection
from mail_verdict.database.models import AccountOwner, Message, PipelineRun, User
from mail_verdict.database.repository import AccountPrefsRepository
from mail_verdict.pipeline import health as pipeline_health
from mail_verdict.pipeline.contracts import (
    StageDefinition,
    StageError,
    StageMisconfigured,
    StageThrottled,
    StageTransient,
    StageUnavailable,
)
from mail_verdict.pipeline.document_validation import DocumentValidationError, validate_document
from mail_verdict.pipeline.registry import STAGE_TYPES
from mail_verdict.pipeline.revisions import (
    PipelineDefinition,
    PipelineRevisionRepository,
    StaleRevisionError,
    definition_to_document,
)
from mail_verdict.pipeline.runner import PipelineRunner
from mail_verdict.pipeline.template import current_or_template
from mail_verdict.rules.assistant import AssistantMessageNotFound, propose
from mail_verdict.settings.credentials import get_provider_credential_repo
from mail_verdict.settings.service import get_settings_service

router = APIRouter(prefix="/pipeline", tags=["pipeline"])
admin_router = APIRouter(
    prefix="/admin/pipeline", tags=["admin"], dependencies=[Depends(require_admin)],
)

# A search call plus a change call is the ordinary exchange; the whole of
# it, retries included, is cut off here.
_ASSISTANT_DEADLINE_SECONDS = 150


async def _current(repo: PipelineRevisionRepository, scope: Scope) -> PipelineDefinition:
    """The caller's current definition, from the template if they have none."""
    return await current_or_template(repo, scope.require_user())


def _stage_out(stage: StageDefinition) -> StageOut:
    return StageOut(
        stage_id=stage.stage_id, type=stage.type, name=stage.name, config=dict(stage.config),
        enabled=stage.enabled, halt=stage.halt,
        accounts=list(stage.accounts) if stage.accounts else None,
    )


async def _document_out(definition: PipelineDefinition, scope: Scope) -> PipelineDocumentOut:
    warnings = await pipeline_health.compute_health(
        get_db_connection(), list(definition.stages), account_ids=sorted(scope.account_ids),
    )
    return PipelineDocumentOut(
        revision=definition.revision, enabled=definition.enabled,
        stages=[_stage_out(s) for s in definition.stages],
        warnings=[
            PipelineHealthEntryOut(
                stage_id=w.stage_id, account_id=w.account_id, reference=w.reference,
                ok=w.ok, detail=w.detail,
            )
            for w in warnings if not w.ok
        ],
    )


def _check_base_revision(current: PipelineDefinition, base_revision: int | None) -> None:
    """A cheap early exit before validation does any work. Not itself
    what makes a stale write 409 -- that is PipelineRevisionRepository
    .append()'s own check, made atomically with the insert it guards.
    Reading `current` here and appending afterwards are still two
    separate round trips, so a concurrent writer can append in the gap
    between this check passing and the eventual append() call; append()
    re-checks at that point and is what actually raises for that case.
    """
    if base_revision is not None and current.revision != base_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                f"base_revision {base_revision} is stale -- current revision "
                f"is {current.revision}"
            ),
        )


async def _announce_pipeline_changed(scope: Scope) -> None:
    """
    Push pipeline.document_changed to the user whose document changed.

    The base_revision/StaleRevisionError machinery in this module exists
    precisely because more than one editor is expected at once -- an
    agent and the UI, most notably -- so a second editor finding out only
    from their own next save getting a 409 is exactly the gap this run is
    closing. The document belongs to a user rather than an account, so the
    event goes on the ring key of the user (see Scope.ring_keys).
    """
    event_ring = get_event_ring()
    if event_ring is None:
        return
    await event_ring.add(scope.require_user(), "pipeline.document_changed", {})


async def _write(
    repo: PipelineRevisionRepository, scope: Scope, *, enabled: bool,
    stages: list[StageDefinition], note: str, expected_base_revision: int | None,
) -> PipelineDefinition:
    """Validate and append a new revision built from already-parsed stage
    definitions, returning the definition as written.

    `expected_base_revision` is forwarded to append() so the check that
    decides whether it is still current and the insert that appends
    happen in the same transaction -- see that method's docstring for
    why a separate pre-check (`_check_base_revision` above) is not
    enough on its own.
    """
    document = definition_to_document(
        PipelineDefinition(revision=0, enabled=enabled, stages=tuple(stages)),
    )
    try:
        validate_document(document, scope=scope)
    except DocumentValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.problems) from None
    try:
        revision = await repo.append(
            scope.require_user(), document, note=note,
            expected_base_revision=expected_base_revision,
        )
    except StaleRevisionError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"base_revision {exc.expected} is stale -- current revision is {exc.actual}",
        ) from None
    await _announce_pipeline_changed(scope)
    return PipelineDefinition(revision=revision, enabled=enabled, stages=tuple(stages))


def _pipeline_runner() -> PipelineRunner:
    """A PipelineRunner built from the same globals the app lifespan
    wires up -- for dry-run test endpoints only, never registered with a
    QueueManager or given a notifier, since it never claims real work."""
    db = get_db_connection()
    return PipelineRunner(
        db, get_settings_service(), get_provider_credential_repo(),
        AccountPrefsRepository(db), get_event_ring(),
    )


@router.get("", response_model=PipelineDocumentOut)
async def get_pipeline(scope: Scope = Depends(current_scope)) -> PipelineDocumentOut:
    """The caller's current pipeline definition, with live folder-resolution
    warnings folded in."""
    definition = await _current(PipelineRevisionRepository(get_db_connection()), scope)
    return await _document_out(definition, scope)


@router.put("", response_model=PipelineDocumentOut)
async def replace_pipeline(
    request: PipelineWriteRequest, scope: Scope = Depends(current_scope),
) -> PipelineDocumentOut:
    """Replace the whole document. A syntax or vocabulary problem is 400;
    a stale base_revision is 409."""
    db = get_db_connection()
    repo = PipelineRevisionRepository(db)
    current = await _current(repo, scope)
    _check_base_revision(current, request.base_revision)

    document = {"enabled": request.enabled, "stages": request.stages}
    try:
        stages = validate_document(document, scope=scope)
    except DocumentValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.problems) from None

    try:
        revision = await repo.append(
            scope.require_user(), document, note="replaced via API",
            expected_base_revision=request.base_revision,
        )
    except StaleRevisionError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"base_revision {exc.expected} is stale -- current revision is {exc.actual}",
        ) from None
    await _announce_pipeline_changed(scope)
    written = PipelineDefinition(revision=revision, enabled=request.enabled, stages=tuple(stages))
    return await _document_out(written, scope)


@router.get("/stage-types", response_model=list[StageTypeOut])
async def list_stage_types() -> list[StageTypeOut]:
    """The registry: what a client can configure each stage type with,
    as a JSON schema -- the same schema registry.build_stage validates
    against, so this can never drift from what a write actually accepts."""
    return [
        StageTypeOut(
            type=stage_cls.type, runs_on=sorted(stage_cls.runs_on),
            schema=stage_cls.config_schema().model_json_schema(),
        )
        for stage_cls in STAGE_TYPES.values()
    ]


@router.post("/stages", response_model=PipelineDocumentOut)
async def create_stage(
    request: StageCreateRequest, scope: Scope = Depends(current_scope),
) -> PipelineDocumentOut:
    """Add one stage to the current definition, at `position` (default:
    append)."""
    db = get_db_connection()
    repo = PipelineRevisionRepository(db)
    current = await _current(repo, scope)
    _check_base_revision(current, request.base_revision)

    if any(s.stage_id == request.stage_id for s in current.stages):
        raise HTTPException(status_code=400, detail=f"stage_id {request.stage_id!r} already exists")

    new_stage = StageDefinition(
        stage_id=request.stage_id, type=request.type, name=request.name or request.stage_id,
        config=request.config, enabled=request.enabled, halt=request.halt,
        accounts=tuple(request.accounts) if request.accounts else None,
    )
    stages = list(current.stages)
    position = (
        len(stages) if request.position is None
        else max(0, min(request.position, len(stages)))
    )
    stages.insert(position, new_stage)

    definition = await _write(
        repo, scope, enabled=current.enabled, stages=stages,
        note=f"added stage {request.stage_id!r}", expected_base_revision=request.base_revision,
    )
    return await _document_out(definition, scope)


@router.patch("/stages/{stage_id}", response_model=PipelineDocumentOut)
async def update_stage(
    stage_id: str, request: StageUpdateRequest, scope: Scope = Depends(current_scope),
) -> PipelineDocumentOut:
    """Partially update one stage. Omitted fields are left as they are."""
    db = get_db_connection()
    repo = PipelineRevisionRepository(db)
    current = await _current(repo, scope)
    _check_base_revision(current, request.base_revision)

    stages = list(current.stages)
    index = next((i for i, s in enumerate(stages) if s.stage_id == stage_id), None)
    if index is None:
        raise HTTPException(
            status_code=404, detail=f"no stage {stage_id!r} in the current pipeline",
        )

    existing = stages[index]
    stages[index] = StageDefinition(
        stage_id=existing.stage_id, type=existing.type,
        name=existing.name if request.name is None else request.name,
        config=existing.config if request.config is None else request.config,
        enabled=existing.enabled if request.enabled is None else request.enabled,
        halt=existing.halt if request.halt is None else request.halt,
        accounts=(
            existing.accounts if request.accounts is None
            else (tuple(request.accounts) if request.accounts else None)
        ),
    )

    definition = await _write(
        repo, scope, enabled=current.enabled, stages=stages, note=f"updated stage {stage_id!r}",
        expected_base_revision=request.base_revision,
    )
    return await _document_out(definition, scope)


@router.delete("/stages/{stage_id}", response_model=PipelineDocumentOut)
async def delete_stage(
    stage_id: str, base_revision: int | None = None, scope: Scope = Depends(current_scope),
) -> PipelineDocumentOut:
    """Remove one stage from the current definition."""
    db = get_db_connection()
    repo = PipelineRevisionRepository(db)
    current = await _current(repo, scope)
    _check_base_revision(current, base_revision)

    stages = [s for s in current.stages if s.stage_id != stage_id]
    if len(stages) == len(current.stages):
        raise HTTPException(
            status_code=404, detail=f"no stage {stage_id!r} in the current pipeline",
        )

    definition = await _write(
        repo, scope, enabled=current.enabled, stages=stages, note=f"removed stage {stage_id!r}",
        expected_base_revision=base_revision,
    )
    return await _document_out(definition, scope)


@router.post("/stages/reorder", response_model=PipelineDocumentOut)
async def reorder_stages(
    request: StageReorderRequest, scope: Scope = Depends(current_scope),
) -> PipelineDocumentOut:
    """Set the full stage order -- `order` must name every current
    stage_id exactly once, so a client cannot silently drop one by typo."""
    db = get_db_connection()
    repo = PipelineRevisionRepository(db)
    current = await _current(repo, scope)
    _check_base_revision(current, request.base_revision)

    by_id = {s.stage_id: s for s in current.stages}
    if set(request.order) != set(by_id) or len(request.order) != len(by_id):
        raise HTTPException(
            status_code=400,
            detail="'order' must name every current stage exactly once",
        )
    stages = [by_id[stage_id] for stage_id in request.order]

    definition = await _write(
        repo, scope, enabled=current.enabled, stages=stages, note="reordered stages",
        expected_base_revision=request.base_revision,
    )
    return await _document_out(definition, scope)


@router.get("/revisions", response_model=list[PipelineRevisionSummary])
async def list_revisions(scope: Scope = Depends(current_scope)) -> list[PipelineRevisionSummary]:
    """Every revision of the caller's document -- metadata only, newest first."""
    db = get_db_connection()
    rows = await PipelineRevisionRepository(db).list_revisions(scope.require_user())
    return [PipelineRevisionSummary(**row) for row in rows]


@router.post("/revisions/{revision}/restore", response_model=PipelineDocumentOut)
async def restore_revision(
    revision: int, scope: Scope = Depends(current_scope),
) -> PipelineDocumentOut:
    """Append a copy of an old revision of the caller's document as the new
    current one -- restore is itself a write, so it stays in history rather
    than rewinding it. A revision that is not the caller's does not exist
    as far as they can tell (404)."""
    db = get_db_connection()
    repo = PipelineRevisionRepository(db)
    old = await repo.get(scope.require_user(), revision)
    if old is None:
        raise HTTPException(status_code=404, detail=f"no revision {revision}")

    definition = await _write(
        repo, scope, enabled=old.enabled, stages=list(old.stages),
        note=f"restored from revision {revision}", expected_base_revision=None,
    )
    return await _document_out(definition, scope)


@router.get("/health", response_model=list[PipelineHealthEntryOut])
async def get_health(scope: Scope = Depends(current_scope)) -> list[PipelineHealthEntryOut]:
    """Every folder reference's resolution against every one of the
    caller's accounts it applies to -- ok and not-ok alike, so a client can
    show the whole picture rather than only failures."""
    db = get_db_connection()
    definition = await _current(PipelineRevisionRepository(db), scope)
    entries = await pipeline_health.compute_health(
        db, list(definition.stages), account_ids=sorted(scope.account_ids),
    )
    return [
        PipelineHealthEntryOut(
            stage_id=e.stage_id, account_id=e.account_id, reference=e.reference,
            ok=e.ok, detail=e.detail,
        )
        for e in entries
    ]


@router.post("/test", response_model=PipelineTestResponse)
async def test_pipeline(
    request: PipelineTestRequest, scope: Scope = Depends(current_scope),
) -> PipelineTestResponse:
    """Dry-run the caller's current pipeline definition against one of
    their messages. Nothing is applied or persisted."""
    db = get_db_connection()
    account_id = await _account_id_for_mail(db, request.message_id, scope)
    runner = _pipeline_runner()
    try:
        result = await runner.dry_run(
            account_id=account_id, message_id=request.message_id, origin=request.origin,
        )
    except StageError as exc:
        # A dry run against a genuinely misconfigured stage (a folder
        # that does not resolve, most commonly) is exactly what this
        # endpoint exists to catch before it happens for real -- report
        # it in the response, never as an unhandled 500.
        return PipelineTestResponse(status="failed", skip_reason=str(exc), trace=[])
    return PipelineTestResponse(
        status=result.status, skip_reason=result.skip_reason, trace=list(result.trace),
    )


@router.post("/stages/{stage_id}/test", response_model=dict[str, Any])
async def test_stage(
    stage_id: str, request: PipelineTestRequest, scope: Scope = Depends(current_scope),
) -> dict[str, Any]:
    """Dry-run one stage of the caller's current definition against one of
    their messages, in isolation from every other stage."""
    db = get_db_connection()
    repo = PipelineRevisionRepository(db)
    current = await _current(repo, scope)
    stage_def = next((s for s in current.stages if s.stage_id == stage_id), None)
    if stage_def is None:
        raise HTTPException(
            status_code=404, detail=f"no stage {stage_id!r} in the current pipeline",
        )

    account_id = await _account_id_for_mail(db, request.message_id, scope)
    runner = _pipeline_runner()
    try:
        return await runner.dry_run_stage(
            account_id=account_id, message_id=request.message_id, stage_def=stage_def,
            origin=request.origin,
        )
    except StageError as exc:
        return {
            "stage_id": stage_def.stage_id, "type": stage_def.type, "matched": False,
            "detail": f"failed: {exc}", "halt": False, "effects": [], "applied": [],
            "usage": None,
        }


async def _account_id_for_mail(
    db: DatabaseConnection, message_id: uuid.UUID, scope: Scope,
) -> uuid.UUID:
    """The account a message is on; 404 if there is no such message, 403 if
    the account is not the caller's."""
    async with db.session() as session:
        result = await session.execute(select(Message.account_id).where(Message.id == message_id))
        account_id = result.scalar_one_or_none()
    if account_id is None:
        raise HTTPException(status_code=404, detail=f"no message {message_id}")
    scope.require_account(account_id)
    return account_id


@router.post("/assistant", response_model=RuleAssistantResponse)
async def rule_assistant(
    body: RuleAssistantRequest, request: Request, scope: Scope = Depends(current_scope),
) -> RuleAssistantResponse | Response:
    """Turn one sentence about an open mail into one proposed change to
    the caller's rules -- any number of rules added, changed, moved or
    removed. The mail must be on one of the caller's accounts.

    Nothing is stored. Accepting the proposal is the client's own `PUT
    /pipeline` with `change.enabled` and `change.stages`, carrying
    `change.base_revision` so rules edited in the meantime get the usual
    409. The exchange stops before
    its next model call once the client has disconnected, and answers with
    an empty 204 nobody reads."""
    db = get_db_connection()
    try:
        async with asyncio.timeout(_ASSISTANT_DEADLINE_SECONDS):
            proposal = await propose(
                db=db, settings_service=get_settings_service(),
                cred_repo=get_provider_credential_repo(), scope=scope,
                message_id=body.message_id, prompt=body.prompt,
                is_disconnected=request.is_disconnected,
            )
    except AssistantMessageNotFound:
        raise HTTPException(status_code=404, detail=f"no message {body.message_id}") from None
    except (StageUnavailable, StageMisconfigured) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except (StageThrottled, StageTransient) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except TimeoutError:
        raise HTTPException(
            status_code=504, detail="the assistant took too long; try again",
        ) from None
    if proposal is None:
        return Response(status_code=204)
    return proposal


class AdminPipelineUserOut(BaseModel):
    """One user's pipeline document as counts -- never a rule, a condition or
    a folder name."""

    user_id: uuid.UUID
    display_name: str
    revision: int | None
    enabled: bool | None
    stages: int
    enabled_stages: int
    changed_at: datetime | None
    failed_runs: int


@admin_router.get("/overview", response_model=list[AdminPipelineUserOut])
async def pipeline_overview() -> list[AdminPipelineUserOut]:
    """Every user's document as counts, with how many pipeline runs on that
    user's accounts ended failed. A user whose document does not exist yet
    (they have not used rules) appears with a null revision."""
    db = get_db_connection()
    summaries = {s["user_id"]: s for s in await PipelineRevisionRepository(db).summaries()}
    async with db.session() as session:
        users = (await session.execute(select(User.id, User.display_name))).all()
        failed = dict(
            (
                await session.execute(
                    select(AccountOwner.user_id, func.count())
                    .join(PipelineRun, PipelineRun.account_id == AccountOwner.account_id)
                    .where(PipelineRun.status == "failed")
                    .group_by(AccountOwner.user_id)
                )
            ).all()
        )
    out: list[AdminPipelineUserOut] = []
    for user_id, display_name in sorted(users, key=lambda u: u.display_name.lower()):
        summary = summaries.get(user_id)
        out.append(AdminPipelineUserOut(
            user_id=user_id, display_name=display_name,
            revision=summary["revision"] if summary else None,
            enabled=summary["enabled"] if summary else None,
            stages=summary["stages"] if summary else 0,
            enabled_stages=summary["enabled_stages"] if summary else 0,
            changed_at=summary["changed_at"] if summary else None,
            failed_runs=failed.get(user_id, 0),
        ))
    return out
