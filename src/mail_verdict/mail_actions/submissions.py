"""
The server's guarantee that a keyed message action is applied once, however
often the request that carries it arrives.

A client acting on mail over a flaky network cannot tell a request that
never arrived from one whose response was lost, so it sends again. Carrying
the same `idempotency_key` makes that safe: the repeat finds the first
request's MessageActionSubmission row and is answered with the response
stored there.

A claim, not a transaction. The action itself runs across several sessions
of its own (a move and a mark-read, a spam ruling), so holding one
transaction -- and one pooled connection -- open around it for the lock's
sake would cost every keyed request two connections at once. Instead the
key's row is inserted first with no response and committed under a
transaction-scoped advisory lock, the action runs, and the response is
written afterwards:

- A repeat arriving while the first is still running finds the claim and
  is told to retry shortly (503), never acted on twice. The running
  request refreshes the claim's heartbeat, so however long the action
  takes, a repeat never mistakes it for abandoned.
- An action that raises, or answers success=false, releases its claim, so
  retrying it once whatever failed is fixed still works -- the same rule
  outbox/submissions.py applies to a send. It releases only its own claim
  (claim_token), never one another request has since taken over.
- A claim whose heartbeat stopped -- the process was killed mid-action --
  is taken over once it is _ABANDONED_AFTER_SECONDS old.
- A key reused for a different request is refused with 409. The request
  is compared without its defaulted fields, so a field added with a
  default in a later release does not turn a retry across the upgrade
  into a refusal.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any, TypeVar

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select, text, update

from mail_verdict.database.models import MessageActionSubmission
from mail_verdict.queue.notify import ReconciliationTimer

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

# A running request refreshes its claim this often...
_HEARTBEAT_SECONDS = 15.0
# ...so a claim silent for this long belongs to a request that is gone.
_ABANDONED_AFTER_SECONDS = 90

# Distinct from every other ReconciliationTimer's lock key in the process
# (tests/unit/test_lock_keys.py checks them all).
_PRUNE_LOCK_KEY = 761_035_000

_PRUNE_INTERVAL_SECONDS = 3600.0


ResponseT = TypeVar("ResponseT", bound=BaseModel)


def request_fingerprint(endpoint: str, target_id: uuid.UUID, request: BaseModel) -> str:
    """
    A digest of what a request asks for, without its key.

    Args:
        endpoint: Which action endpoint the request reached
        target_id: The message or account id from the request path
        request: The request body

    Returns:
        A hex digest equal for two requests asking for the same thing
    """
    body = request.model_dump(mode="json", exclude={"idempotency_key"}, exclude_defaults=True)
    canonical = json.dumps([endpoint, str(target_id), body], sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def run_once(
    db: DatabaseConnection,
    idempotency_key: uuid.UUID,
    fingerprint: str,
    response_model: type[ResponseT],
    apply: Callable[[], Awaitable[ResponseT]],
) -> ResponseT:
    """
    Apply a keyed action once, or answer a repeat with the first response.

    Args:
        db: Database connection
        idempotency_key: The caller's key for this action
        fingerprint: request_fingerprint() of the request
        response_model: The endpoint's response model, to rebuild a replay
        apply: Performs the action and returns its response

    Returns:
        The action's response, fresh or replayed

    Raises:
        HTTPException: 409 if the key was used for a different request, 503
            if a request with this key is still being applied
    """
    token = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))"),
            {"name": f"message-action:{idempotency_key}"},
        )
        previous = await session.scalar(
            select(MessageActionSubmission).where(
                MessageActionSubmission.idempotency_key == idempotency_key,
            )
        )
        if previous is not None:
            if previous.fingerprint != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="idempotency_key was already used for a different request.",
                )
            if previous.response is not None:
                return response_model.model_validate(previous.response)
            abandoned = await session.scalar(
                select(
                    MessageActionSubmission.heartbeat_at
                    < func.now() - timedelta(seconds=_ABANDONED_AFTER_SECONDS)
                ).where(MessageActionSubmission.idempotency_key == idempotency_key)
            )
            if not abandoned:
                raise HTTPException(
                    status_code=503,
                    detail="A request with this idempotency_key is still being applied.",
                    headers={"Retry-After": "1"},
                )
            await session.execute(
                update(MessageActionSubmission)
                .where(MessageActionSubmission.idempotency_key == idempotency_key)
                .values(created_at=func.now(), heartbeat_at=func.now(), claim_token=token)
            )
        else:
            session.add(
                MessageActionSubmission(
                    idempotency_key=idempotency_key, fingerprint=fingerprint, claim_token=token,
                )
            )

    heartbeat = asyncio.create_task(_beat(db, idempotency_key, token))
    try:
        response = await apply()
    except BaseException:
        await _stop(heartbeat)
        await _release(db, idempotency_key, token)
        raise
    await _stop(heartbeat)
    if not _succeeded(response):
        await _release(db, idempotency_key, token)
        return response

    async with db.session() as session:
        await session.execute(
            update(MessageActionSubmission)
            .where(
                MessageActionSubmission.idempotency_key == idempotency_key,
                MessageActionSubmission.claim_token == token,
            )
            .values(response=response.model_dump(mode="json"), completed_at=func.now())
        )
    return response


async def _beat(db: DatabaseConnection, idempotency_key: uuid.UUID, token: uuid.UUID) -> None:
    """Keep a claim visibly alive for as long as its action runs."""
    while True:
        await asyncio.sleep(_HEARTBEAT_SECONDS)
        async with db.session() as session:
            await session.execute(
                update(MessageActionSubmission)
                .where(
                    MessageActionSubmission.idempotency_key == idempotency_key,
                    MessageActionSubmission.claim_token == token,
                )
                .values(heartbeat_at=func.now())
            )


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def _succeeded(response: Any) -> bool:
    """Whether a response reports the action as applied -- an endpoint
    answering 200 with success=false did nothing worth replaying."""
    return bool(getattr(response, "success", True))


async def _release(db: DatabaseConnection, idempotency_key: uuid.UUID, token: uuid.UUID) -> None:
    """Drop this request's own unfinished claim, so the key can be used again."""
    async with db.session() as session:
        await session.execute(
            delete(MessageActionSubmission).where(
                MessageActionSubmission.idempotency_key == idempotency_key,
                MessageActionSubmission.claim_token == token,
                MessageActionSubmission.response.is_(None),
            )
        )


async def prune_submissions_once(db: DatabaseConnection, retention: timedelta) -> int:
    """
    Delete submissions older than the retention window.

    Args:
        db: Database connection
        retention: How long a key is remembered

    Returns:
        Rows deleted
    """
    async with db.session() as session:
        result = await session.execute(
            delete(MessageActionSubmission).where(
                MessageActionSubmission.created_at < func.now() - retention,
            )
        )
    deleted = int(getattr(result, "rowcount", 0) or 0)
    if deleted:
        logger.info("Pruned message action submissions", extra={"deleted": deleted})
    return deleted


def build_submission_prune_timer(
    db: DatabaseConnection, retention: timedelta,
) -> ReconciliationTimer:
    """The advisory-locked periodic pass that forgets old keys."""

    async def _callback() -> None:
        await prune_submissions_once(db, retention)

    return ReconciliationTimer(db, _PRUNE_LOCK_KEY, _callback, _PRUNE_INTERVAL_SECONDS)
