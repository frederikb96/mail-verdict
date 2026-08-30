"""
RunContext: everything a stage gets that is not the message itself.

Built fresh per run by the runner (pipeline/runner.py) and never
constructed by a stage -- a stage that could reach the database directly
could not be dry-run or unit-tested without one, which is the whole point
of the effect-returning contract in pipeline/contracts.py.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from mail_verdict.core.errors import ProviderUnavailableError
from mail_verdict.database.models import Folder, Verdict, VerdictSource
from mail_verdict.pipeline.contracts import (
    JsonValue,
    StageOutcome,
    StageThrottled,
    StageUnavailable,
)
from mail_verdict.queue.circuit import CircuitBreaker

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.settings.credentials import ProviderCredentialRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VerdictView:
    """The verdict a run should treat as current for its message -- latest
    user_feedback if one exists, otherwise latest ai/rule verdict. Never a
    naive "latest by created_at" across sources: a user's correction must
    not be overwritten by a model call that was already in flight when
    they made it, however the two rows' timestamps happen to land."""

    is_spam: bool
    source: VerdictSource
    reasoning: str | None
    created_at: object


@dataclass(frozen=True)
class MessageHistory:
    """Durable prior verdicts for this msg_key, independent of mail_id --
    what survives a UIDVALIDITY resync or retention purge deleting the
    row a verdict was originally recorded against."""

    has_ai_verdict: bool


class BoundLog:
    """A logger with run_id/stage_id merged into every call's `extra`."""

    def __init__(self, base: logging.Logger, **bound: object) -> None:
        self._base = base
        self._bound = bound

    def _extra(self, extra: dict[str, object] | None) -> dict[str, object]:
        return {**self._bound, **(extra or {})}

    def info(self, msg: str, *, extra: dict[str, object] | None = None) -> None:
        self._base.info(msg, extra=self._extra(extra))

    def warning(self, msg: str, *, extra: dict[str, object] | None = None) -> None:
        self._base.warning(msg, extra=self._extra(extra))

    def error(self, msg: str, *, extra: dict[str, object] | None = None) -> None:
        self._base.error(msg, extra=self._extra(extra))


class FolderResolver:
    """Resolves a Move effect's folder reference against one account's
    folders. Every lookup is scoped to `account_id` at construction --
    two accounts can never resolve into each other's folders."""

    def __init__(self, db: DatabaseConnection, account_id: uuid.UUID) -> None:
        self._db = db
        self._account_id = account_id

    async def resolve(
        self, *, folder_name: str | None = None, special_use: str | None = None,
        folder_id: uuid.UUID | None = None,
    ) -> uuid.UUID | None:
        """
        Resolve a folder reference to its current id.

        Returns:
            The folder id, or None if nothing matches -- the caller raises
            StageMisconfigured, since "which reference, on which account"
            is only known at the call site.
        """
        async with self._db.session() as session:
            if folder_id is not None:
                result = await session.execute(
                    select(Folder.id).where(
                        Folder.id == folder_id, Folder.account_id == self._account_id,
                        Folder.deleted_at.is_(None),
                    )
                )
                return result.scalar_one_or_none()
            if folder_name is not None:
                result = await session.execute(
                    select(Folder.id).where(
                        Folder.account_id == self._account_id, Folder.imap_name == folder_name,
                        Folder.deleted_at.is_(None),
                    )
                )
                return result.scalar_one_or_none()
            if special_use is not None:
                result = await session.execute(
                    select(Folder.id).where(
                        Folder.account_id == self._account_id,
                        Folder.special_use == special_use,
                        Folder.deleted_at.is_(None),
                    ).limit(1)
                )
                return result.scalar_one_or_none()
        return None


class _NullCircuit:
    """A circuit breaker that never trips and never persists anything --
    used only when a ModelGateway is built with no database connection."""

    async def is_available(self) -> bool:
        return True

    async def status(self) -> Any:
        raise AssertionError("status() should never be called while is_available() is True")

    async def record_success(self) -> None:
        return None

    async def record_unavailable(self, *, reason: str, probe_interval: timedelta) -> None:
        return None

    async def record_backoff(self, *, retry_after: timedelta, reason: str | None = None) -> None:
        return None


class ModelGateway:
    """
    Schema-validated model calls with retries, resolved per call rather
    than captured at construction.

    Every stage-level model call goes through here so retry, provider
    resolution and the circuit breaker are implemented once. A stage never
    imports structured_llm directly.
    """

    def __init__(
        self,
        db: DatabaseConnection | None,
        cred_repo: ProviderCredentialRepository,
        retry_config: Any,
    ) -> None:
        self._db = db
        self._cred_repo = cred_repo
        self._retry_config = retry_config
        self._circuit: CircuitBreaker | _NullCircuit = _NullCircuit()

    async def structured_call(
        self,
        *,
        provider: str,
        model: str,
        effort: str | None,
        max_tokens: int,
        schema_name: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, JsonValue],
        validate: Any = None,
    ) -> tuple[dict[str, Any], float]:
        """
        Issue one strict-schema request.

        The circuit breaker is keyed by provider name alone, so it is
        shared by every caller of that provider -- a future embedding
        worker calling OpenAI trips and clears the same breaker a
        classify stage's calls do.

        Returns:
            (parsed response, latency in milliseconds)

        Raises:
            StageUnavailable: no key configured, or the provider rejected
                the credential outright
            StageThrottled: rate limited
            StageTransient: any other failure, including retries exhausted
        """
        from mail_verdict.pipeline.contracts import StageMisconfigured

        if provider not in ("anthropic", "openai"):
            raise StageMisconfigured(f"Unknown ai.provider {provider!r}")

        # db is None only in a test building a ModelGateway with no database
        # (see tests/unit/test_llm_live.py) -- circuit tracking is a
        # production safety net, not something a call against a real
        # provider needs in order to prove the request/response shape.
        self._circuit = (
            CircuitBreaker(self._db, provider) if self._db is not None else _NullCircuit()
        )
        if not await self._circuit.is_available():
            status = await self._circuit.status()
            if status.state.value == "suspended":
                raise StageUnavailable(f"{provider} circuit suspended: {status.reason}")
            raise StageThrottled(f"{provider} circuit open: {status.reason}")

        from mail_verdict.core.structured_llm import (
            call_anthropic_structured,
            call_openai_structured,
            resolve_client,
        )

        started = time.monotonic()
        try:
            client = await resolve_client(provider, self._cred_repo)
        except ProviderUnavailableError as exc:
            await self._circuit.record_unavailable(
                reason=str(exc), probe_interval=timedelta(minutes=5),
            )
            raise StageUnavailable(str(exc)) from exc

        try:
            if provider == "anthropic":
                data = await call_anthropic_structured(
                    client, model, effort, max_tokens, system_prompt, user_prompt,
                    schema, self._retry_config, validate=validate,
                )
            else:
                data = await call_openai_structured(
                    client, model, effort, max_tokens, schema_name, system_prompt,
                    user_prompt, schema, self._retry_config, validate=validate,
                )
        except Exception as exc:
            await self._map_and_raise(provider, exc)
            raise  # unreachable, _map_and_raise always raises

        await self._circuit.record_success()
        latency_ms = (time.monotonic() - started) * 1000
        return data, latency_ms

    async def _map_and_raise(self, provider: str, exc: Exception) -> None:
        """Translate a provider SDK exception into the stage vocabulary,
        recording the outcome on the shared circuit breaker."""
        from mail_verdict.pipeline.contracts import StageTransient

        name = type(exc).__name__
        if name == "AuthenticationError":
            await self._circuit.record_unavailable(
                reason=f"{provider} rejected the API key", probe_interval=timedelta(minutes=5),
            )
            raise StageUnavailable(f"{provider} rejected the API key") from exc
        if name == "RateLimitError":
            retry_after = getattr(exc, "retry_after", None)
            delay = timedelta(seconds=retry_after) if retry_after else timedelta(seconds=30)
            await self._circuit.record_backoff(retry_after=delay, reason=f"{provider} rate limited")
            raise StageThrottled(f"{provider} rate limited", retry_after=delay) from exc
        await self._circuit.record_backoff(
            retry_after=timedelta(seconds=30), reason=f"{provider} call failed: {exc}",
        )
        raise StageTransient(str(exc)) from exc


@dataclass
class RunContext:
    """Everything a stage gets that is not the message."""

    run_id: uuid.UUID
    account_id: uuid.UUID
    origin: Literal["live", "historical"]
    apply: bool
    settings: Mapping[str, Any]
    trace: Sequence[StageOutcome]
    facts: Mapping[str, JsonValue]
    verdict: VerdictView | None
    history: MessageHistory
    folders: FolderResolver
    models: ModelGateway
    log: BoundLog
    # The per-account "enable spam detection" toggle (account_prefs.spam_enabled),
    # read once per run by the runner so the classify stage stays free of its
    # own database access -- see pipeline/stages/classify.py.
    account_spam_enabled: bool = False

    def with_trace_entry(self, outcome: StageOutcome) -> RunContext:
        """A copy with `outcome` appended to the trace and its facts merged
        in -- how the runner threads state from one stage to the next."""
        return _replace(
            self, trace=(*self.trace, outcome), facts={**self.facts, **outcome.facts},
        )

    def with_verdict(self, verdict: VerdictView) -> RunContext:
        """A copy with a freshly recorded verdict projected in, so a later
        stage in the same run (the move-spam match stage, most notably)
        sees it without a re-read."""
        return _replace(self, verdict=verdict)


def _replace(ctx: RunContext, **changes: object) -> RunContext:
    from dataclasses import replace

    return replace(ctx, **changes)  # type: ignore[arg-type]


async def current_verdict_for_mail(
    db: DatabaseConnection, mail_id: uuid.UUID,
) -> VerdictView | None:
    """
    The verdict a run should treat as current: latest user_feedback if one
    exists, otherwise latest ai/rule verdict, by created_at.

    Args:
        db: Database connection
        mail_id: The message's current row id

    Returns:
        VerdictView, or None if no verdict has ever been recorded
    """
    async with db.session() as session:
        feedback_result = await session.execute(
            select(Verdict)
            .where(Verdict.mail_id == mail_id, Verdict.source == VerdictSource.USER_FEEDBACK)
            .order_by(Verdict.created_at.desc())
            .limit(1)
        )
        row = feedback_result.scalar_one_or_none()
        if row is None:
            other_result = await session.execute(
                select(Verdict)
                .where(Verdict.mail_id == mail_id, Verdict.source != VerdictSource.USER_FEEDBACK)
                .order_by(Verdict.created_at.desc())
                .limit(1)
            )
            row = other_result.scalar_one_or_none()
        if row is None:
            return None
        return VerdictView(
            is_spam=row.is_spam, source=row.source, reasoning=row.reasoning,
            created_at=row.created_at,
        )


async def history_for_msg_key(
    db: DatabaseConnection, *, account_id: uuid.UUID, msg_key: str, from_addr: str | None,
) -> MessageHistory:
    """
    Whether an AI verdict already exists for this (account, msg_key,
    from_addr) -- the never-classify-twice gate, keyed on the durable
    identity rather than on mail_id so it survives retention purge and a
    UIDVALIDITY resync.
    """
    async with db.session() as session:
        result = await session.execute(
            select(Verdict.id)
            .where(
                Verdict.account_id == account_id,
                Verdict.msg_key == msg_key,
                Verdict.source == VerdictSource.AI,
                Verdict.from_addr == from_addr if from_addr else Verdict.from_addr.is_(None),
            )
            .limit(1)
        )
        return MessageHistory(has_ai_verdict=result.scalar_one_or_none() is not None)
