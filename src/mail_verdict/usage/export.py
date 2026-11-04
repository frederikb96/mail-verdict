"""
Pushing llm_usage rows to an Elasticsearch data stream, and pruning them.

Every pass sends the rows after the stored position (usage_export_state)
as `_bulk` `create` operations, each with the `_id` `mail-verdict-<id>`, so
a row sent twice answers 409 and counts as delivered. A batch stops at the
first document Elasticsearch refuses: the position moves past the ones
delivered before it, and the refused one is retried on the next pass.

A row is sent only once it is `_ROW_SETTLE` old. Ids are taken at insert
and become visible at commit, so a concurrent write can show a higher id
before a lower one; a position that passed the lower one while it was
invisible would never send it.

Without the `elastic_usage` credential (settings/credentials.py) nothing
is sent and recording goes on. The outcome of the last pass is stored on
the state row and reported by GET /api/settings/providers; a stream that
stops arriving is for the receiving side to notice, not for an alert here.

Rows older than `retention_days` are deleted on every pass, sent or not.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import delete, func, select

from mail_verdict.database.models import LlmUsage, UsageExportState
from mail_verdict.queue.notify import ReconciliationTimer

if TYPE_CHECKING:
    from mail_verdict.config.loader import UsageExportConfig
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.settings.credentials import ProviderCredentialRepository

logger = logging.getLogger(__name__)

# Distinct from every other ReconciliationTimer's lock key in the process
# (tests/unit/test_lock_keys.py checks them all).
_USAGE_EXPORT_LOCK_KEY = 761_035_600

CREDENTIAL = "elastic_usage"
DESTINATION = "elasticsearch"
APP = "mail-verdict"
# Far longer than any transaction writing a usage row stays open.
_ROW_SETTLE = timedelta(seconds=60)


@dataclass(frozen=True)
class ExportStatus:
    """What the API reports about the export: when a request last delivered
    documents, the error of the last request if it failed, and how many
    rows wait to be sent."""

    last_success_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    pending: int


def document(row: LlmUsage) -> dict[str, Any]:
    """The Elasticsearch document of one usage row."""
    return {
        "@timestamp": row.created_at.astimezone(timezone.utc).isoformat(),
        "llm": {"usage": {
            "app": APP,
            "id": str(row.id),
            "purpose": row.purpose,
            "provider": row.provider,
            "model": row.model,
            "tokens": {
                "input": row.input_tokens,
                "cache_read": row.cache_read_tokens,
                "cache_write": row.cache_write_tokens,
                "output": row.output_tokens,
            },
        }},
    }


def document_id(row: LlmUsage) -> str:
    """The `_id` a row is created under."""
    return f"{APP}-{row.id}"


async def _position(db: DatabaseConnection) -> int:
    """The last delivered row's id, 0 before the first delivery."""
    async with db.session() as session:
        state = await session.get(UsageExportState, DESTINATION)
        if state is None:
            session.add(UsageExportState(name=DESTINATION, last_id=0))
            return 0
        return state.last_id


async def _settled_batch(db: DatabaseConnection, after_id: int, limit: int) -> list[LlmUsage]:
    """The rows after `after_id`, oldest first, up to the first one younger
    than `_ROW_SETTLE`."""
    async with db.session() as session:
        now = (await session.execute(select(func.now()))).scalar_one()
        rows = list((await session.execute(
            select(LlmUsage).where(LlmUsage.id > after_id).order_by(LlmUsage.id).limit(limit)
        )).scalars())
    settled: list[LlmUsage] = []
    for row in rows:
        if row.created_at > now - _ROW_SETTLE:
            break
        settled.append(row)
    return settled


async def _bulk(
    client: httpx.AsyncClient, data_stream: str, rows: list[LlmUsage],
) -> tuple[int, str | None]:
    """
    Create `rows` in one `_bulk` request.

    Returns:
        (how many from the start were delivered, a 409 counting as
        delivered; why the first refused one was refused, or None)
    """
    body = "".join(
        json.dumps({"create": {"_id": document_id(row)}}) + "\n"
        + json.dumps(document(row)) + "\n"
        for row in rows
    )
    response = await client.post(
        f"/{data_stream}/_bulk", content=body,
        headers={"Content-Type": "application/x-ndjson"},
    )
    if response.status_code >= 400:
        return 0, f"Elasticsearch answered HTTP {response.status_code}: {response.text[:300]}"
    delivered = 0
    for item in response.json().get("items", []):
        outcome = item.get("create", {})
        # With the stream's failure store on, a rejected document answers
        # 201 and lands there instead; that is a refusal too.
        if outcome.get("status") not in (200, 201, 409) or outcome.get("failure_store") == "used":
            error = outcome.get("error") or {}
            return delivered, (
                f"Elasticsearch refused {outcome.get('_id')}: "
                f"{error.get('type')}: {error.get('reason')}"
            )
        delivered += 1
    if delivered < len(rows):
        return delivered, f"Elasticsearch answered {delivered} of {len(rows)} items"
    return delivered, None


async def _record_outcome(db: DatabaseConnection, *, last_id: int, error: str | None) -> None:
    """Store the position and how the request that reached it went."""
    async with db.session() as session:
        state = await session.get(UsageExportState, DESTINATION)
        if state is None:
            state = UsageExportState(name=DESTINATION, last_id=0)
            session.add(state)
        state.last_id = last_id
        now = datetime.now(timezone.utc)
        if error is None:
            state.last_success_at = now
            state.last_error = None
            state.last_error_at = None
        else:
            state.last_error = error
            state.last_error_at = now


async def export_once(
    db: DatabaseConnection,
    cred_repo: ProviderCredentialRepository,
    cfg: UsageExportConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """
    Send every settled row not yet delivered.

    Args:
        db: Database connection
        cred_repo: Where the Elasticsearch API key is stored
        cfg: The export's config section
        transport: Replaces the network, for tests

    Returns:
        How many documents were delivered
    """
    if not cfg.elasticsearch_url:
        return 0
    key = await cred_repo.resolve_key(CREDENTIAL)
    if not key:
        return 0
    sent = 0
    position = await _position(db)
    async with httpx.AsyncClient(
        base_url=cfg.elasticsearch_url, transport=transport,
        timeout=httpx.Timeout(cfg.request_timeout_seconds),
        headers={"Authorization": f"ApiKey {key}"},
    ) as client:
        while True:
            rows = await _settled_batch(db, position, cfg.batch_size)
            if not rows:
                return sent
            try:
                delivered, error = await _bulk(client, cfg.data_stream, rows)
            except (httpx.HTTPError, ValueError) as exc:
                delivered, error = 0, f"{type(exc).__name__}: {exc}"
            if delivered:
                position = rows[delivered - 1].id
            sent += delivered
            if error is not None:
                await _record_outcome(db, last_id=position, error=error)
                logger.warning("Usage export failed", extra={"error": error, "sent": sent})
                return sent
            await _record_outcome(db, last_id=position, error=None)
            if len(rows) < cfg.batch_size:
                return sent


async def prune_once(db: DatabaseConnection, retention: timedelta) -> int:
    """Delete usage rows older than `retention`. Returns how many."""
    async with db.session() as session:
        result = await session.execute(
            delete(LlmUsage).where(LlmUsage.created_at < func.now() - retention)
        )
    return int(getattr(result, "rowcount", 0) or 0)


async def export_status(db: DatabaseConnection) -> ExportStatus:
    """The last pass's outcome and how many rows wait to be sent."""
    async with db.session() as session:
        state = await session.get(UsageExportState, DESTINATION)
        last_id = state.last_id if state is not None else 0
        pending = (await session.execute(
            select(func.count()).select_from(LlmUsage).where(LlmUsage.id > last_id)
        )).scalar_one()
    return ExportStatus(
        last_success_at=state.last_success_at if state is not None else None,
        last_error=state.last_error if state is not None else None,
        last_error_at=state.last_error_at if state is not None else None,
        pending=int(pending),
    )


def build_usage_export_timer(
    db: DatabaseConnection, cred_repo: ProviderCredentialRepository, cfg: UsageExportConfig,
) -> ReconciliationTimer:
    """The advisory-locked periodic pass that exports and prunes usage rows
    -- one per process, safe with more than one replica."""
    retention = timedelta(days=cfg.retention_days)

    async def _callback() -> None:
        await export_once(db, cred_repo, cfg)
        await prune_once(db, retention)

    return ReconciliationTimer(db, _USAGE_EXPORT_LOCK_KEY, _callback, cfg.interval_seconds)
