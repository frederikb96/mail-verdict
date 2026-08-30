"""
The pipeline runner: claims a `pipeline_runs` row, executes the current
pipeline definition against it, and leaves the row in a terminal status.

The three invariants the design rests on, each closing a family of races:

  - The run reads the world at execution time. The claimed row carries
    only (account_id, msg_key, message_id) -- everything else about the
    message is re-read from the database, so a message that moved or was
    expunged between enqueue and execution is simply seen as it is now.
  - Every effect is applied with a guard predicate and the rowcount is the
    truth (pipeline/effects.py) -- never assumed from having issued the
    UPDATE.
  - Effects are projected onto the in-memory MessageView as they apply, so
    a later stage in the same run sees intended state without a re-read.

A crash mid-run re-executes from the first stage; every effect is
idempotent (see pipeline/contracts.py), so this is safe by construction
and there is no per-stage checkpoint to reconstruct.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy import Table, select, text

from mail_verdict.core.retry import RetryConfig
from mail_verdict.database.models import Message, PipelineRun, VerdictSource
from mail_verdict.pipeline import effect_codec
from mail_verdict.pipeline.context import (
    BoundLog,
    FolderResolver,
    ModelGateway,
    RunContext,
    VerdictView,
    current_verdict_for_mail,
    history_for_msg_key,
)
from mail_verdict.pipeline.contracts import (
    RecordVerdict,
    StageMisconfigured,
    StageOutcome,
    StageThrottled,
    StageTransient,
    StageUnavailable,
)
from mail_verdict.pipeline.effects import AppliedEffect, apply_effects
from mail_verdict.pipeline.message_view import MessageView, load_message_view
from mail_verdict.pipeline.neighbors import NeighborService
from mail_verdict.pipeline.registry import build_stage
from mail_verdict.pipeline.revisions import PipelineRevisionRepository
from mail_verdict.queue.backoff import compute_backoff
from mail_verdict.queue.manager import QueueManager
from mail_verdict.queue.work_queue import WorkQueue
from mail_verdict.queue.worker_loop import default_worker_loop

if TYPE_CHECKING:
    import asyncio

    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.database.repository import AccountPrefsRepository
    from mail_verdict.pipeline.contracts import StageDefinition
    from mail_verdict.queue.notify import WorkQueueNotifier
    from mail_verdict.settings.credentials import ProviderCredentialRepository
    from mail_verdict.settings.service import SettingsService

logger = logging.getLogger(__name__)

QUEUE_NAME = "pipeline"

# Folders where a message never enters the pipeline. Kept in sync with
# pipeline/enqueue.py's enqueue-time gate; re-checked here because the
# message may have moved into one of these between enqueue and execution.
_SKIP_FOLDER_SPECIAL_USE = frozenset({"sent", "drafts", "trash", "junk", "archive"})

_DEFAULT_UNAVAILABLE_DELAY_S = 60.0


@dataclass
class _RunResult:
    """What one run's execution produced, before it is written back."""

    status: str
    skip_reason: str | None
    trace: tuple[dict[str, Any], ...]
    model_calls: int
    pipeline_rev: int | None = None
    halted_at_stage: str | None = None


class PipelineRunner:
    """Owns the `pipeline` queue's worker body: claim, execute, terminate."""

    def __init__(
        self,
        db: DatabaseConnection,
        settings_service: SettingsService,
        cred_repo: ProviderCredentialRepository,
        account_prefs_repo: AccountPrefsRepository,
        event_ring: EventRing | None,
        notifier: WorkQueueNotifier | None = None,
    ) -> None:
        self._db = db
        self._settings = settings_service
        self._cred_repo = cred_repo
        self._account_prefs_repo = account_prefs_repo
        self._event_ring = event_ring
        self._notifier = notifier
        self._revisions = PipelineRevisionRepository(db)
        self._table = cast(Table, PipelineRun.__table__)
        self._work_queue = WorkQueue(db, self._table)

    def register(self, manager: QueueManager) -> WorkQueue:
        """Register the `pipeline` queue with the shared QueueManager."""
        return manager.register(
            QUEUE_NAME, self._table, self._worker_body, circuit_name=self._circuit_name,
        )

    def _circuit_name(self) -> str:
        """The breaker a classify stage's calls actually trip.

        `ModelGateway` names its breaker for the provider, which is the
        live `ai.provider` setting rather than the queue -- so this is
        resolved per call, not captured at registration.
        """
        return str(self._settings.get("ai")["provider"]).lower()

    async def dry_run(
        self, *, account_id: uuid.UUID, message_id: uuid.UUID,
        origin: Literal["live", "historical"] = "live",
    ) -> _RunResult:
        """
        Execute the current pipeline definition against one message
        without applying anything or touching pipeline_runs -- the
        POST /api/pipeline/test endpoint.

        A synthetic row, never persisted: apply is forced False regardless
        of what a caller passes, since a dry run that could write would
        not be one. msg_key is left blank -- _load_view only falls back to
        it when message_id fails to resolve, and a blank key simply
        resolves to nothing rather than to some other message.

        Args:
            account_id: Account the message belongs to
            message_id: The message's current messages.id
            origin: 'live' or 'historical' -- which stages are eligible

        Returns:
            The same _RunResult a real run produces, trace included
        """
        row = {
            "id": uuid.uuid4(), "account_id": account_id, "message_id": message_id,
            "msg_key": "", "origin": origin, "apply": False,
        }
        return await self._execute_run(row)

    async def dry_run_stage(
        self, *, account_id: uuid.UUID, message_id: uuid.UUID,
        stage_def: StageDefinition, origin: Literal["live", "historical"] = "live",
    ) -> dict[str, Any]:
        """
        Execute one stage definition -- not necessarily one already saved
        in a revision -- against one message, without applying anything.
        POST /api/pipeline/stages/{id}/test, and also what a client
        validates a not-yet-saved stage edit against before writing it.

        Unlike dry_run(), this never reads the current pipeline
        definition: the stage under test runs alone, seeing the same
        verdict/history/neighbours a real run would load, but no facts
        from any other stage (there are none, since nothing ran before
        it).

        Returns:
            One trace entry, in the same shape pipeline_runs.trace stores
        """
        view = await self._load_view(
            {"message_id": message_id, "account_id": account_id, "msg_key": ""},
        )
        if view is None:
            return {
                "stage_id": stage_def.stage_id, "type": stage_def.type, "matched": False,
                "detail": "skipped: message gone", "halt": False, "effects": [], "applied": [],
                "usage": None,
            }

        settings_snapshot = self._settings.get_all()
        retry_config = RetryConfig.from_settings(settings_snapshot.get("retry", {}))
        account_prefs = await self._account_prefs_repo.get_by_account(account_id)

        ctx = RunContext(
            run_id=uuid.uuid4(), account_id=account_id, origin=origin, apply=False,
            settings=settings_snapshot, trace=(), facts={},
            verdict=await current_verdict_for_mail(self._db, view.message_id),
            history=await history_for_msg_key(
                self._db, account_id=account_id, msg_key=view.msg_key, from_addr=view.from_addr,
            ),
            folders=FolderResolver(self._db, account_id),
            neighbors=NeighborService(self._db, account_id),
            models=ModelGateway(self._db, self._cred_repo, retry_config),
            log=BoundLog(logger, run_id="dry-run-stage"),
            account_spam_enabled=bool(account_prefs and account_prefs.spam_enabled),
        )

        stage = build_stage(stage_def)
        if origin not in type(stage).runs_on:
            return _trace_entry(stage_def, StageOutcome(
                matched=False, detail=f"skipped: does not run on {origin} mail",
            ))
        if stage_def.accounts is not None and account_id not in stage_def.accounts:
            return _trace_entry(
                stage_def, StageOutcome(matched=False, detail="skipped: out of account scope"),
            )

        outcome = await stage.execute(view, ctx)
        _, applied = await apply_effects(
            self._db, view, outcome.effects, apply=False,
            folders=ctx.folders, event_ring=None, stage_id=stage_def.stage_id,
        )
        return _trace_entry(stage_def, outcome, applied=applied)

    def _pipeline_settings(self) -> dict[str, Any]:
        return self._settings.get("pipeline") if self._settings.has_category("pipeline") else {}

    async def _worker_body(self, worker_id: str, stop_event: asyncio.Event) -> None:
        settings = self._pipeline_settings()
        lease_seconds = float(settings.get("lease_seconds", 120))
        poll_interval = float(settings.get("poll_interval_seconds", 2.0))
        wake_event = self._notifier.event_for(QUEUE_NAME) if self._notifier else None

        await default_worker_loop(
            self._work_queue, worker_id=worker_id, stop_event=stop_event,
            batch_size=1, lease_seconds=lease_seconds,
            handle_item=self._handle_item, wake_event=wake_event, poll_interval=poll_interval,
        )

    async def _handle_item(self, row: Mapping[str, Any]) -> None:
        item_id = row["id"]
        worker_id = str(row["claimed_by"])
        settings = self._pipeline_settings()

        try:
            outcome = await self._execute_run(row)
        except StageMisconfigured as exc:
            await self._finish_failed(row, str(exc), stage_id=exc.stage_id)
            return
        except StageUnavailable as exc:
            delay = float(settings.get("unavailable_probe_seconds", _DEFAULT_UNAVAILABLE_DELAY_S))
            await self._work_queue.release_untouched(item_id, worker_id=worker_id)
            await self._push_next_attempt(item_id, delay)
            logger.warning("Run suspended: %s", exc, extra={"run_id": str(item_id)})
            return
        except StageThrottled as exc:
            delay = (
                exc.retry_after.total_seconds() if exc.retry_after else _DEFAULT_UNAVAILABLE_DELAY_S
            )
            await self._work_queue.release_untouched(item_id, worker_id=worker_id)
            await self._push_next_attempt(item_id, delay)
            return
        except StageTransient as exc:
            await self._retry_transient(row, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 -- unknown failure, same retry-then-fail path
            await self._retry_transient(row, f"unexpected: {exc}")
            return

        await self._finish_ok(row, outcome)

    async def _execute_run(self, row: Mapping[str, Any]) -> _RunResult:
        """Execute the current pipeline definition against one claimed
        row. Raises a Stage* exception on any stage failure; the caller
        decides retry/suspend/fail from the exception type."""
        run_id = row["id"]
        account_id = row["account_id"]
        origin = row["origin"]
        apply_writes = bool(row["apply"])

        definition = await self._revisions.current()
        if definition is None or not definition.enabled:
            return _RunResult(
                status="skipped", skip_reason="pipeline disabled", trace=(), model_calls=0,
            )

        view = await self._load_view(row)
        if view is None:
            return _RunResult(status="skipped", skip_reason="message gone", trace=(), model_calls=0)
        if view.is_draft or (view.folder.special_use or "") in _SKIP_FOLDER_SPECIAL_USE:
            return _RunResult(
                status="skipped", skip_reason="out of scope (folder or draft)",
                trace=(), model_calls=0,
            )

        settings_snapshot = self._settings.get_all()
        retry_config = RetryConfig.from_settings(settings_snapshot.get("retry", {}))
        account_prefs = await self._account_prefs_repo.get_by_account(account_id)

        ctx = RunContext(
            run_id=run_id, account_id=account_id, origin=origin, apply=apply_writes,
            settings=settings_snapshot, trace=(), facts={},
            verdict=await current_verdict_for_mail(self._db, view.message_id),
            history=await history_for_msg_key(
                self._db, account_id=account_id, msg_key=view.msg_key, from_addr=view.from_addr,
            ),
            folders=FolderResolver(self._db, account_id),
            neighbors=NeighborService(self._db, account_id),
            models=ModelGateway(self._db, self._cred_repo, retry_config),
            log=BoundLog(logger, run_id=str(run_id)),
            account_spam_enabled=bool(account_prefs and account_prefs.spam_enabled),
        )

        trace: list[dict[str, Any]] = []
        model_calls = 0
        halted_at: str | None = None

        for stage_def in definition.stages:
            if not stage_def.enabled:
                continue
            stage = build_stage(stage_def)
            if origin not in type(stage).runs_on:
                trace.append(_trace_entry(stage_def, StageOutcome(
                    matched=False, detail=f"skipped: does not run on {origin} mail",
                )))
                continue
            if stage_def.accounts is not None and account_id not in stage_def.accounts:
                trace.append(_trace_entry(stage_def, StageOutcome(
                    matched=False, detail="skipped: out of account scope",
                )))
                continue

            outcome = await stage.execute(view, ctx)
            if outcome.usage is not None:
                model_calls += 1

            view, applied = await apply_effects(
                self._db, view, outcome.effects, apply=apply_writes,
                folders=ctx.folders, event_ring=self._event_ring, stage_id=stage_def.stage_id,
            )
            trace.append(_trace_entry(stage_def, outcome, applied=applied))
            ctx = ctx.with_trace_entry(outcome)
            ctx = _project_verdict(ctx, outcome, applied)

            if outcome.halt:
                halted_at = stage_def.stage_id
                break

        return _RunResult(
            status="done", skip_reason=None, trace=tuple(trace),
            model_calls=model_calls, pipeline_rev=definition.revision, halted_at_stage=halted_at,
        )

    async def _load_view(self, row: Mapping[str, Any]) -> MessageView | None:
        message_id = row.get("message_id")
        account_id = row["account_id"]
        msg_key = row["msg_key"]

        async with self._db.session() as session:
            if message_id is not None:
                view = await load_message_view(session, message_id)
                if view is not None:
                    return view

            # message_id is stale (a UIDVALIDITY resync assigned a new
            # row) -- msg_key is the durable identity, so re-resolve by
            # the header it holds when it looks like one.
            if not msg_key.startswith("sha256:"):
                result = await session.execute(
                    select(Message.id).where(
                        Message.account_id == account_id, Message.message_id == msg_key,
                        Message.expunged_at.is_(None),
                    ).limit(1)
                )
                resolved_id = result.scalar_one_or_none()
                if resolved_id is not None:
                    return await load_message_view(session, resolved_id)
        return None

    async def _push_next_attempt(self, item_id: uuid.UUID, delay_seconds: float) -> None:
        async with self._db.session() as session:
            await session.execute(
                text(
                    "UPDATE pipeline_runs SET next_attempt_at = now() + make_interval(secs => :s) "
                    "WHERE id = :id AND status = 'pending'"
                ),
                {"id": item_id, "s": delay_seconds},
            )

    async def _retry_transient(self, row: Mapping[str, Any], error: str) -> None:
        settings = self._pipeline_settings()
        max_attempts = int(settings.get("max_attempts", 5))
        item_id, worker_id = row["id"], str(row["claimed_by"])
        if row["attempts"] >= max_attempts:
            await self._work_queue.fail(item_id, worker_id=worker_id, last_error=error)
            return
        delay = compute_backoff(
            row["attempts"],
            base_seconds=float(settings.get("base_delay_seconds", 2.0)),
            cap_seconds=float(settings.get("max_delay_seconds", 60.0)),
        )
        await self._work_queue.retry(
            item_id, worker_id=worker_id,
            next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=delay),
            last_error=error,
        )

    async def _finish_failed(
        self, row: Mapping[str, Any], error: str, *, stage_id: str | None = None,
    ) -> None:
        if stage_id is not None:
            async with self._db.session() as session:
                await session.execute(
                    text("UPDATE pipeline_runs SET failed_stage = :stage_id WHERE id = :id"),
                    {"id": row["id"], "stage_id": stage_id},
                )
        await self._work_queue.fail(row["id"], worker_id=str(row["claimed_by"]), last_error=error)

    async def _finish_ok(self, row: Mapping[str, Any], outcome: _RunResult) -> None:
        async with self._db.session() as session:
            await session.execute(
                text(
                    "UPDATE pipeline_runs SET trace = CAST(:trace AS jsonb), "
                    "skip_reason = :skip_reason, "
                    "pipeline_rev = :pipeline_rev, halted_at_stage = :halted_at_stage, "
                    "model_calls = model_calls + :model_calls, "
                    "started_at = COALESCE(started_at, now()), finished_at = now() "
                    "WHERE id = :id"
                ),
                {
                    "id": row["id"], "trace": json.dumps(list(outcome.trace)),
                    "skip_reason": outcome.skip_reason, "pipeline_rev": outcome.pipeline_rev,
                    "halted_at_stage": outcome.halted_at_stage, "model_calls": outcome.model_calls,
                },
            )
        await self._work_queue.complete(
            row["id"], worker_id=str(row["claimed_by"]), status=outcome.status,
        )
        if self._event_ring is not None:
            await self._event_ring.add(
                row["account_id"], "pipeline.run_finished",
                {
                    "run_id": str(row["id"]), "status": outcome.status,
                    "halted_at": outcome.halted_at_stage,
                },
            )


def _project_verdict(
    ctx: RunContext, outcome: StageOutcome, applied: list[AppliedEffect],
) -> RunContext:
    """If this stage's RecordVerdict effect actually applied, project it
    onto the context so a later stage in the same run (move-spam, most
    notably) sees it without a re-read."""
    for effect, applied_effect in zip(outcome.effects, applied, strict=True):
        if applied_effect.applied and isinstance(effect, RecordVerdict):
            return ctx.with_verdict(VerdictView(
                is_spam=effect.is_spam, source=VerdictSource.AI,
                reasoning=effect.reasoning, created_at=None,
            ))
    return ctx


def _trace_entry(
    stage_def: StageDefinition,
    outcome: StageOutcome,
    *,
    applied: list[AppliedEffect] | None = None,
) -> dict[str, Any]:
    return {
        "stage_id": stage_def.stage_id,
        "type": stage_def.type,
        "matched": outcome.matched,
        "detail": outcome.detail,
        "halt": outcome.halt,
        "effects": [effect_codec.effect_to_dict(e) for e in outcome.effects],
        "applied": [
            {
                "effect": effect_codec.effect_to_dict(a.effect),
                "applied": a.applied, "detail": a.detail,
            }
            for a in (applied or [])
        ],
        "usage": (
            {"model": outcome.usage.model, "latency_ms": outcome.usage.latency_ms}
            if outcome.usage else None
        ),
    }


__all__ = ["QUEUE_NAME", "PipelineRunner"]
