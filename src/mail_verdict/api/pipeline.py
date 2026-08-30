"""
Pipeline configuration API -- the surface an agent (or the /pipeline UI
page) edits the pipeline through, instead of inserting into
pipeline_revisions by hand.

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

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from mail_verdict.api.events import get_event_ring
from mail_verdict.api.schemas import (
    PipelineDocumentOut,
    PipelineHealthEntryOut,
    PipelineRevisionSummary,
    PipelineTestRequest,
    PipelineTestResponse,
    PipelineWriteRequest,
    StageCreateRequest,
    StageOut,
    StageReorderRequest,
    StageTypeOut,
    StageUpdateRequest,
)
from mail_verdict.database.connection import DatabaseConnection, get_db_connection
from mail_verdict.database.models import Message
from mail_verdict.database.repository import AccountPrefsRepository, AccountRepository
from mail_verdict.pipeline import health as pipeline_health
from mail_verdict.pipeline.contracts import StageDefinition, StageError
from mail_verdict.pipeline.document_validation import DocumentValidationError, validate_document
from mail_verdict.pipeline.registry import STAGE_TYPES
from mail_verdict.pipeline.revisions import (
    PipelineDefinition,
    PipelineRevisionRepository,
    StaleRevisionError,
    definition_to_document,
)
from mail_verdict.pipeline.runner import PipelineRunner
from mail_verdict.settings.credentials import get_provider_credential_repo
from mail_verdict.settings.service import get_settings_service

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

_EMPTY_DEFINITION = PipelineDefinition(revision=0, enabled=True, stages=())


async def _current(repo: PipelineRevisionRepository) -> PipelineDefinition:
    """The current definition, or a synthetic empty one (revision 0) if
    no revision has ever been written -- a fresh, unmigrated database."""
    definition = await repo.current()
    return definition if definition is not None else _EMPTY_DEFINITION


def _stage_out(stage: StageDefinition) -> StageOut:
    return StageOut(
        stage_id=stage.stage_id, type=stage.type, name=stage.name, config=dict(stage.config),
        enabled=stage.enabled, halt=stage.halt,
        accounts=list(stage.accounts) if stage.accounts else None,
    )


async def _document_out(definition: PipelineDefinition) -> PipelineDocumentOut:
    db = get_db_connection()
    account_ids = [a.id for a in await AccountRepository(db).get_all()]
    warnings = await pipeline_health.compute_health(
        db, list(definition.stages), account_ids=account_ids,
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


async def _write(
    repo: PipelineRevisionRepository, *, enabled: bool, stages: list[StageDefinition],
    note: str, expected_base_revision: int | None,
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
        validate_document(document)
    except DocumentValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.problems) from None
    try:
        revision = await repo.append(
            document, note=note, expected_base_revision=expected_base_revision,
        )
    except StaleRevisionError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"base_revision {exc.expected} is stale -- current revision is {exc.actual}",
        ) from None
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
async def get_pipeline() -> PipelineDocumentOut:
    """The current pipeline definition, with live folder-resolution
    warnings folded in."""
    db = get_db_connection()
    definition = await _current(PipelineRevisionRepository(db))
    return await _document_out(definition)


@router.put("", response_model=PipelineDocumentOut)
async def replace_pipeline(request: PipelineWriteRequest) -> PipelineDocumentOut:
    """Replace the whole document. A syntax or vocabulary problem is 400;
    a stale base_revision is 409."""
    db = get_db_connection()
    repo = PipelineRevisionRepository(db)
    current = await _current(repo)
    _check_base_revision(current, request.base_revision)

    document = {"enabled": request.enabled, "stages": request.stages}
    try:
        stages = validate_document(document)
    except DocumentValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.problems) from None

    try:
        revision = await repo.append(
            document, note="replaced via API", expected_base_revision=request.base_revision,
        )
    except StaleRevisionError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"base_revision {exc.expected} is stale -- current revision is {exc.actual}",
        ) from None
    written = PipelineDefinition(revision=revision, enabled=request.enabled, stages=tuple(stages))
    return await _document_out(written)


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
async def create_stage(request: StageCreateRequest) -> PipelineDocumentOut:
    """Add one stage to the current definition, at `position` (default:
    append)."""
    db = get_db_connection()
    repo = PipelineRevisionRepository(db)
    current = await _current(repo)
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
        repo, enabled=current.enabled, stages=stages,
        note=f"added stage {request.stage_id!r}", expected_base_revision=request.base_revision,
    )
    return await _document_out(definition)


@router.patch("/stages/{stage_id}", response_model=PipelineDocumentOut)
async def update_stage(stage_id: str, request: StageUpdateRequest) -> PipelineDocumentOut:
    """Partially update one stage. Omitted fields are left as they are."""
    db = get_db_connection()
    repo = PipelineRevisionRepository(db)
    current = await _current(repo)
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
        repo, enabled=current.enabled, stages=stages, note=f"updated stage {stage_id!r}",
        expected_base_revision=request.base_revision,
    )
    return await _document_out(definition)


@router.delete("/stages/{stage_id}", response_model=PipelineDocumentOut)
async def delete_stage(stage_id: str, base_revision: int | None = None) -> PipelineDocumentOut:
    """Remove one stage from the current definition."""
    db = get_db_connection()
    repo = PipelineRevisionRepository(db)
    current = await _current(repo)
    _check_base_revision(current, base_revision)

    stages = [s for s in current.stages if s.stage_id != stage_id]
    if len(stages) == len(current.stages):
        raise HTTPException(
            status_code=404, detail=f"no stage {stage_id!r} in the current pipeline",
        )

    definition = await _write(
        repo, enabled=current.enabled, stages=stages, note=f"removed stage {stage_id!r}",
        expected_base_revision=base_revision,
    )
    return await _document_out(definition)


@router.post("/stages/reorder", response_model=PipelineDocumentOut)
async def reorder_stages(request: StageReorderRequest) -> PipelineDocumentOut:
    """Set the full stage order -- `order` must name every current
    stage_id exactly once, so a client cannot silently drop one by typo."""
    db = get_db_connection()
    repo = PipelineRevisionRepository(db)
    current = await _current(repo)
    _check_base_revision(current, request.base_revision)

    by_id = {s.stage_id: s for s in current.stages}
    if set(request.order) != set(by_id) or len(request.order) != len(by_id):
        raise HTTPException(
            status_code=400,
            detail="'order' must name every current stage exactly once",
        )
    stages = [by_id[stage_id] for stage_id in request.order]

    definition = await _write(
        repo, enabled=current.enabled, stages=stages, note="reordered stages",
        expected_base_revision=request.base_revision,
    )
    return await _document_out(definition)


@router.get("/revisions", response_model=list[PipelineRevisionSummary])
async def list_revisions() -> list[PipelineRevisionSummary]:
    """Every revision's metadata, newest first."""
    db = get_db_connection()
    rows = await PipelineRevisionRepository(db).list_revisions()
    return [PipelineRevisionSummary(**row) for row in rows]


@router.post("/revisions/{revision}/restore", response_model=PipelineDocumentOut)
async def restore_revision(revision: int) -> PipelineDocumentOut:
    """Append a copy of an old revision as the new current one -- restore
    is itself a write, so it stays in history rather than rewinding it."""
    db = get_db_connection()
    repo = PipelineRevisionRepository(db)
    old = await repo.get(revision)
    if old is None:
        raise HTTPException(status_code=404, detail=f"no revision {revision}")

    definition = await _write(
        repo, enabled=old.enabled, stages=list(old.stages),
        note=f"restored from revision {revision}", expected_base_revision=None,
    )
    return await _document_out(definition)


@router.get("/health", response_model=list[PipelineHealthEntryOut])
async def get_health() -> list[PipelineHealthEntryOut]:
    """Every folder reference's resolution against every account it
    applies to -- ok and not-ok alike, so a client can show the whole
    picture rather than only failures."""
    db = get_db_connection()
    definition = await _current(PipelineRevisionRepository(db))
    account_ids = [a.id for a in await AccountRepository(db).get_all()]
    entries = await pipeline_health.compute_health(
        db, list(definition.stages), account_ids=account_ids,
    )
    return [
        PipelineHealthEntryOut(
            stage_id=e.stage_id, account_id=e.account_id, reference=e.reference,
            ok=e.ok, detail=e.detail,
        )
        for e in entries
    ]


@router.post("/test", response_model=PipelineTestResponse)
async def test_pipeline(request: PipelineTestRequest) -> PipelineTestResponse:
    """Dry-run the current pipeline definition against one existing
    message. Nothing is applied or persisted."""
    db = get_db_connection()
    account_id = await _account_id_for_mail(db, request.message_id)
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
async def test_stage(stage_id: str, request: PipelineTestRequest) -> dict[str, Any]:
    """Dry-run one stage of the current definition against one existing
    message, in isolation from every other stage."""
    db = get_db_connection()
    repo = PipelineRevisionRepository(db)
    current = await _current(repo)
    stage_def = next((s for s in current.stages if s.stage_id == stage_id), None)
    if stage_def is None:
        raise HTTPException(
            status_code=404, detail=f"no stage {stage_id!r} in the current pipeline",
        )

    account_id = await _account_id_for_mail(db, request.message_id)
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


async def _account_id_for_mail(db: DatabaseConnection, message_id: uuid.UUID) -> uuid.UUID:
    async with db.session() as session:
        result = await session.execute(select(Message.account_id).where(Message.id == message_id))
        account_id = result.scalar_one_or_none()
    if account_id is None:
        raise HTTPException(status_code=404, detail=f"no message {message_id}")
    return account_id
