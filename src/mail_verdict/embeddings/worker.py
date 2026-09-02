"""
The embedding queue's worker body and its registration with QueueManager.

register_embeddings() is the seam this module hands to whoever composes
the application lifespan: it wires the queue, the worker body and the
reconciliation timer together and hands back handles to start and stop,
without touching the lifespan itself.

The one constraint from the design that is easy to get backwards: a worker
must not hold a database session across the provider call. Every method on
WorkQueue and EmbeddingRepository opens and closes its own session, so as
long as this module never holds one open while awaiting embed_batch, that
constraint holds by construction rather than by discipline.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

from mail_verdict.core.errors import ProviderUnavailableError
from mail_verdict.database.models import MessageEmbedding
from mail_verdict.database.repository import MessageRepository
from mail_verdict.embeddings.content import build_embedding_input
from mail_verdict.embeddings.provider import DEFAULT_EMBEDDING_MODEL, resolve_embedding_provider
from mail_verdict.embeddings.repository import EmbeddingRepository
from mail_verdict.queue.backoff import compute_backoff
from mail_verdict.queue.circuit import CircuitBreaker, CircuitState
from mail_verdict.queue.manager import QueueManager
from mail_verdict.queue.notify import ReconciliationTimer
from mail_verdict.queue.worker_loop import heartbeat_while

if TYPE_CHECKING:
    from sqlalchemy import Table

    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.settings.credentials import ProviderCredentialRepository
    from mail_verdict.settings.service import SettingsService

logger = logging.getLogger(__name__)

QUEUE_NAME = "embeddings"
CIRCUIT_NAME = "openai"
LEASE_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 2.0

# How long a suspended breaker blocks the next probe attempt -- matches
# the probe_interval passed to record_unavailable below, so a worker that
# wins the right to probe is also the worker whose next claim actually is
# that probe.
PROBE_INTERVAL = timedelta(minutes=5)

# Arbitrary, stable advisory lock key for the backfill reconciler -- must
# not collide with queue/manager.py's own reclaim-timer key or any other
# reconciliation timer sharing this database. tests/unit/test_lock_keys.py
# derives every *_LOCK_KEY constant in the package and asserts they are
# pairwise distinct, so a future collision fails a test rather than
# blocking a pause/resume behind an unrelated backfill batch.
_BACKFILL_LOCK_KEY = 761_034_223


class EmbeddingComponents:
    """Handles the composed lifespan needs to start and stop, once
    register_embeddings() has wired everything together."""

    def __init__(self, backfill_timer: ReconciliationTimer) -> None:
        self._backfill_timer = backfill_timer

    async def start(self) -> None:
        """Start the backfill reconciler. The queue's own worker
        supervisor is started by QueueManager.start(), not here."""
        await self._backfill_timer.start()

    async def stop(self) -> None:
        """Stop the backfill reconciler."""
        await self._backfill_timer.stop()


def register_embeddings(
    queue_manager: QueueManager,
    db: DatabaseConnection,
    cred_repo: ProviderCredentialRepository,
    settings_service: SettingsService,
    *,
    backfill_interval_seconds: float = 30.0,
) -> EmbeddingComponents:
    """
    Register the embedding queue and its backfill reconciler.

    Does not start anything -- the caller composes this into its own
    lifespan, calling queue_manager.start()/stop() (which now also drives
    this queue's worker supervisor) and the returned components'
    start()/stop() around it.

    Args:
        queue_manager: The application's QueueManager
        db: Database connection
        cred_repo: Provider API key repository
        settings_service: Application settings service
        backfill_interval_seconds: How often the reconciler enqueues one
            more batch of missing embeddings

    Returns:
        Handles for the caller's lifespan to start/stop
    """
    embedding_repo = EmbeddingRepository(db)
    message_repo = MessageRepository(db)
    circuit = CircuitBreaker(db, CIRCUIT_NAME)

    async def worker_body(worker_id: str, stop_event: asyncio.Event) -> None:
        await _run_worker(
            queue_manager, worker_id, stop_event,
            embedding_repo, message_repo, cred_repo, settings_service, circuit,
        )

    queue_manager.register(
        QUEUE_NAME, cast("Table", MessageEmbedding.__table__), worker_body,
        circuit_name=CIRCUIT_NAME,
    )

    async def _reconcile() -> None:
        settings = settings_service.get("semantic")
        if not settings.get("enabled", True):
            return
        model = str(settings.get("model", DEFAULT_EMBEDDING_MODEL))
        # How many rows this sweep inserts, not how many the worker claims
        # per iteration -- the worker always claims one row at a time (see
        # _run_worker), independent of this setting.
        batch_size = int(settings.get("batch_size", 64))
        candidates, inserted = await embedding_repo.enqueue_missing_batch(
            model=model, batch_size=max(batch_size, 1) * 4,
        )
        if inserted:
            logger.info(
                "Embedding backfill enqueued messages",
                extra={"model": model, "candidates": candidates, "inserted": inserted},
            )

    backfill_timer = ReconciliationTimer(
        db, _BACKFILL_LOCK_KEY, _reconcile, backfill_interval_seconds,
    )
    return EmbeddingComponents(backfill_timer)


async def _run_worker(
    queue_manager: QueueManager,
    worker_id: str,
    stop_event: asyncio.Event,
    embedding_repo: EmbeddingRepository,
    message_repo: MessageRepository,
    cred_repo: ProviderCredentialRepository,
    settings_service: SettingsService,
    circuit: CircuitBreaker,
) -> None:
    """
    Claim/process loop for one worker task.

    Deliberately not queue/worker_loop.py's default_worker_loop: that loop
    claims continuously once entered, and this one has to check the shared
    provider circuit before every claim rather than only once at startup,
    so a circuit that opens mid-run stops this worker from claiming
    immediately instead of on its next restart.

    Claims exactly one row per iteration, the same way pipeline/runner.py
    does -- never a batch under one shared lease. `heartbeat_while` only
    ever extends the lease of the row it currently wraps, so a batch claim
    of N would leave every row but the one in flight sitting on its
    original, un-renewed lease: the reconciliation timer reclaims those
    without refunding the attempt claiming them already spent, and this
    same worker -- there does not need to be a second one -- goes on to
    re-claim and re-run whatever it already finished. Provider calls here
    are billed per message regardless (embed_batch is always called with
    one text), so a wider claim would only have bought fewer claim-query
    round trips, at the cost of exactly this failure mode.
    """
    work_queue = queue_manager.work_queue(QUEUE_NAME)

    while not stop_event.is_set():
        if not await circuit.is_available():
            status = await circuit.status()
            if status.state != CircuitState.SUSPENDED or not await circuit.try_probe(
                probe_interval=PROBE_INTERVAL,
            ):
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue
            # Won the right to probe: the one row claimed below is itself
            # the probe call. A success closes the breaker via
            # record_success in _handle_one, the ordinary success path --
            # there is no separate probe call.

        claimed = await work_queue.claim_batch(
            worker_id=worker_id, batch_size=1, lease_seconds=LEASE_SECONDS,
        )
        if not claimed:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue

        row = claimed[0]
        if stop_event.is_set():
            await work_queue.release_untouched(row["id"], worker_id=worker_id)
            continue
        async with heartbeat_while(
            work_queue, row["id"], worker_id=worker_id, lease_seconds=LEASE_SECONDS,
        ):
            await _handle_one(
                row, worker_id, work_queue, embedding_repo, message_repo,
                cred_repo, settings_service, circuit,
            )


async def _handle_one(
    row: Mapping[str, Any],
    worker_id: str,
    work_queue: Any,
    embedding_repo: EmbeddingRepository,
    message_repo: MessageRepository,
    cred_repo: ProviderCredentialRepository,
    settings_service: SettingsService,
    circuit: CircuitBreaker,
) -> None:
    """
    Process one claimed message_embeddings row to a terminal state.

    No session is held open across the provider call: the message is read
    in its own session (via MessageRepository), the embedding request
    holds no session at all, and the result is written in a final, freshly
    opened session -- via EmbeddingRepository.write_result or .fail,
    never work_queue's own generic terminal transitions, since both of
    those also gate this message's pipeline run in the same transaction
    (see pipeline/enqueue.enqueue_pipeline_run_if_live_eligible).
    """
    item_id: uuid.UUID = row["id"]
    account_id: uuid.UUID = row["account_id"]
    message_id: uuid.UUID | None = row["message_id"]
    model: str = row["model"]

    if message_id is None:
        await embedding_repo.fail(
            item_id, worker_id=worker_id, last_error="no message_id on row",
            settings_service=settings_service,
        )
        return

    message = await message_repo.get_by_id(account_id, message_id)
    if message is None:
        await embedding_repo.fail(
            item_id, worker_id=worker_id, last_error="message no longer exists",
            settings_service=settings_service,
        )
        return

    settings = settings_service.get("semantic")
    content_chars = int(settings.get("content_chars", 2000))
    provider_name = str(settings.get("provider", "openai"))

    embedding_input = build_embedding_input(
        subject=message.subject, from_addr=message.from_addr,
        body_text=message.body_text, body_html=message.body_html,
        is_truncated=message.is_truncated, content_chars=content_chars,
    )

    try:
        provider = resolve_embedding_provider(provider_name, cred_repo)
        vectors = await provider.embed_batch([embedding_input.text], model=model)
    except ProviderUnavailableError as exc:
        await circuit.record_unavailable(reason=str(exc), probe_interval=PROBE_INTERVAL)
        await work_queue.release_untouched(item_id, worker_id=worker_id)
        return
    except Exception as exc:
        name = type(exc).__name__
        if name in _SHARED_THROTTLE_ERRORS:
            # A shared-resource throttle, not this item's fault: every
            # queued item waits out the same circuit backoff, and none of
            # them burns an attempt over it -- the same uncapped refund
            # ProviderUnavailableError gets above.
            await circuit.record_backoff(retry_after=timedelta(seconds=30), reason=str(exc))
            await work_queue.release_untouched(item_id, worker_id=worker_id)
        elif _is_retryable(exc):
            # Unlike a throttle, this class of error (a connection drop, a
            # 5xx, a timeout) can recur for one payload specifically, so it
            # must reach a terminal state eventually rather than retry
            # forever -- counted against the row's own attempts and capped,
            # mirroring how the pipeline runner caps StageTransient.
            await circuit.record_backoff(retry_after=timedelta(seconds=30), reason=str(exc))
            await _retry_or_fail(
                item_id, worker_id=worker_id, row=row, error=str(exc),
                work_queue=work_queue, embedding_repo=embedding_repo,
                settings=settings, settings_service=settings_service,
            )
        else:
            await embedding_repo.fail(
                item_id, worker_id=worker_id, last_error=str(exc),
                settings_service=settings_service,
            )
        return

    await circuit.record_success()
    wrote = await embedding_repo.write_result(
        item_id, worker_id=worker_id, embedding=vectors[0], model=model,
        content_level=embedding_input.content_level, source_hash=embedding_input.source_hash,
        settings_service=settings_service,
    )
    if not wrote:
        logger.warning(
            "Embedding computed but claim was lost before it could be written",
            extra={"item_id": str(item_id)},
        )


# A rate limit is a shared-resource throttle: every item queued behind it
# is equally blameless, so it is refunded and retried uncapped rather than
# counted against any one row's attempts -- see ProviderUnavailableError's
# handling above, which this mirrors.
_SHARED_THROTTLE_ERRORS = frozenset({"RateLimitError"})


def _is_retryable(exc: Exception) -> bool:
    """Whether an embedding-provider exception is worth retrying at all,
    rather than failing permanently on the spot.

    Matches on class name rather than importing `openai`'s exception
    types at module load -- resolve_embedding_provider only imports the
    client library when a provider actually needs it, and this keeps that
    property rather than forcing the import back in at the top of the
    module.
    """
    name = type(exc).__name__
    return name in _SHARED_THROTTLE_ERRORS | {
        "APIConnectionError", "InternalServerError", "APITimeoutError",
    }


async def _retry_or_fail(
    item_id: uuid.UUID,
    *,
    worker_id: str,
    row: Mapping[str, Any],
    error: str,
    work_queue: Any,
    embedding_repo: EmbeddingRepository,
    settings: Mapping[str, Any],
    settings_service: SettingsService,
) -> None:
    """
    Requeue a retryable-but-possibly-item-specific failure with backoff,
    counting it against the row's own attempts.

    The dead-letter path this class of error needs: a connection drop or a
    5xx that keeps recurring for one particular payload must eventually
    reach 'failed' rather than retry forever, unlike a shared-resource
    throttle where every item is equally blameless.

    Args:
        item_id: Row to requeue or fail
        worker_id: Must match the row's current claimant
        row: The claimed row, for its current `attempts` count
        error: Recorded as the row's last_error either way
        work_queue: Queue to requeue against
        embedding_repo: Repository to fail through, so a permanent
            failure still opens the pipeline gate (see its own docstring)
        settings: The "semantic" settings category, already read by the
            caller
        settings_service: Passed through to embedding_repo.fail for its
            own live-eligibility check
    """
    max_attempts = int(settings.get("max_attempts", 5))
    if row["attempts"] >= max_attempts:
        await embedding_repo.fail(
            item_id, worker_id=worker_id, last_error=error, settings_service=settings_service,
        )
        return
    delay = compute_backoff(
        row["attempts"],
        base_seconds=float(settings.get("base_delay_seconds", 2.0)),
        cap_seconds=float(settings.get("max_delay_seconds", 60.0)),
    )
    await work_queue.retry(
        item_id, worker_id=worker_id,
        next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=delay),
        last_error=error,
    )
