"""
The webhook queue's worker: claims one delivery at a time and makes one
HTTP request for it, so deliveries leave in order, one after another.

A rule's `webhook` effect only inserts a `webhook_deliveries` row
(webhooks/repository.py); nothing in a rule pass ever waits on the network.
This worker owns the request, and the outcome of each response class:

  - 2xx: delivered. Terminal; the row is never claimed again, which is what
    keeps a mail from being sent twice after a success.
  - 5xx, 408, 429, a network error or a timeout: retried with jittered
    backoff until `webhooks.max_attempts`, then failed.
  - any other response (a 4xx, a redirect): failed at once. The receiver has
    said the request itself is wrong; sending it again changes nothing.

Every failure that ends a delivery raises a `webhook_failed` alert, so a
mail that never arrived is visible rather than silent. A failed row stays in
the table and blocks re-enqueueing the same mail; re-queueing is explicit
(api/webhooks.py).

A request that times out, or whose worker dies after the receiver processed
it, is retried and can therefore arrive twice; a receiver with no
de-duplication of its own sees that. Nothing narrows it further without the
receiver's cooperation.

No secret value, rendered header, response body or exception text from the
request path is ever logged or stored: error text is built from the
exception's class and the status code only, since a transport error can
quote the header it rejected.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

import httpx
from sqlalchemy import text

from mail_verdict.database.models import WebhookDelivery
from mail_verdict.database.repository import AlertRepository
from mail_verdict.queue.backoff import compute_backoff
from mail_verdict.queue.manager import QueueManager
from mail_verdict.queue.worker_loop import default_worker_loop
from mail_verdict.settings.secret_store import SecretUnavailableError
from mail_verdict.webhooks.repository import QUEUE_NAME
from mail_verdict.webhooks.spec import referenced_secrets, render_headers

if TYPE_CHECKING:
    from sqlalchemy import Table

    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.config.loader import WebhooksConfig
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.push.vapid import VapidKeyRepository
    from mail_verdict.settings.secret_store import SecretRepository

logger = logging.getLogger(__name__)

_RETRYABLE_STATUSES = frozenset({408, 429})
_BODY_CONTENT_TYPE = "message/rfc822"


@dataclass(frozen=True)
class _Source:
    raw: bytes | None
    received_at: datetime | None
    subject: str | None


@dataclass(frozen=True)
class _Outcome:
    """How one attempt ended. `kind` is done, skipped, transient or permanent."""

    kind: str
    detail: str
    http_status: int | None = None


def register_webhooks(
    queue_manager: QueueManager,
    db: DatabaseConnection,
    secret_repo: SecretRepository,
    cfg: WebhooksConfig,
    event_ring: EventRing | None,
    vapid_repo: VapidKeyRepository | None,
) -> None:
    """Register the webhooks queue with the shared QueueManager; nothing
    starts until queue_manager.start(). One worker by default (a fresh
    queue's own concurrency), which is what makes deliveries sequential."""

    async def worker_body(worker_id: str, stop_event: asyncio.Event) -> None:
        work_queue = queue_manager.work_queue(QUEUE_NAME)

        async def handle_item(row: Mapping[str, Any]) -> None:
            await handle_delivery(
                row, worker_id, work_queue, db, secret_repo, cfg, event_ring, vapid_repo,
            )

        await default_worker_loop(
            work_queue, worker_id=worker_id, stop_event=stop_event, batch_size=1,
            lease_seconds=cfg.lease_seconds, handle_item=handle_item,
            poll_interval=cfg.poll_interval_seconds, max_attempts=cfg.max_attempts,
        )

    queue_manager.register(
        QUEUE_NAME, cast("Table", WebhookDelivery.__table__), worker_body,
    )


async def handle_delivery(
    row: Mapping[str, Any],
    worker_id: str,
    work_queue: Any,
    db: DatabaseConnection,
    secret_repo: SecretRepository,
    cfg: WebhooksConfig,
    event_ring: EventRing | None,
    vapid_repo: VapidKeyRepository | None,
) -> None:
    """Make the request for one claimed delivery and leave the row terminal
    or pending-for-retry."""
    item_id: uuid.UUID = row["id"]
    try:
        source = await _load_source(db, row)
        if source is None:
            outcome = _Outcome("skipped", "message gone")
        else:
            outcome = await _attempt(row, source, secret_repo, cfg)
    except Exception as exc:  # noqa: BLE001 -- anything unexpected is retried, then failed
        outcome = _Outcome("transient", f"unexpected {type(exc).__name__}")
        logger.warning(
            "Webhook delivery attempt crashed",
            extra={"delivery_id": str(item_id), "error": type(exc).__name__},
        )

    if outcome.kind == "done":
        await _mark_done(db, item_id, worker_id, outcome.http_status)
        return
    if outcome.kind == "skipped":
        await work_queue.complete(item_id, worker_id=worker_id, status="skipped")
        await _record_status(db, item_id, None, outcome.detail)
        return
    if outcome.kind == "transient" and row["attempts"] < cfg.max_attempts:
        delay = compute_backoff(
            row["attempts"], base_seconds=cfg.base_delay_seconds, cap_seconds=cfg.max_delay_seconds,
        )
        await work_queue.retry(
            item_id, worker_id=worker_id,
            next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=delay),
            last_error=outcome.detail,
        )
        await _record_status(db, item_id, outcome.http_status, None)
        return

    reason = (
        outcome.detail if outcome.kind == "permanent"
        else f"{outcome.detail} (gave up after {row['attempts']} attempts)"
    )
    if await work_queue.fail(item_id, worker_id=worker_id, last_error=reason):
        await _record_status(db, item_id, outcome.http_status, None)
        await _raise_failure_alert(
            db, row, source.subject if source else None, reason, event_ring, vapid_repo,
        )


async def _attempt(
    row: Mapping[str, Any], source: _Source, secret_repo: SecretRepository, cfg: WebhooksConfig,
) -> _Outcome:
    config: Mapping[str, Any] = row["config"]
    if source.raw is None:
        return _Outcome(
            "permanent", "the raw message was not stored (it exceeded the mirror's size limit)",
        )
    if len(source.raw) > cfg.max_body_bytes:
        return _Outcome(
            "permanent", f"message is {len(source.raw)} bytes, over the {cfg.max_body_bytes} limit",
        )

    templates: Mapping[str, str] = config.get("headers") or {}
    try:
        secrets = await secret_repo.resolve_many(referenced_secrets(templates))
    except SecretUnavailableError as exc:
        return _Outcome("permanent", str(exc))
    headers = render_headers(templates, secrets)
    if any(not _is_header_safe(v) for v in headers.values()):
        return _Outcome("permanent", "a header value, after substituting secrets, is not valid")
    headers["Content-Type"] = _BODY_CONTENT_TYPE

    url = httpx.URL(config["url"])
    param = config.get("received_at_param")
    if param and source.received_at is not None:
        url = url.copy_add_param(param, source.received_at.astimezone(timezone.utc).isoformat())

    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            response = await asyncio.wait_for(
                client.request(config["method"], url, headers=headers, content=source.raw),
                timeout=cfg.request_timeout_seconds,
            )
    except asyncio.TimeoutError:
        return _Outcome("transient", "request timed out")
    except httpx.HTTPError as exc:
        return _Outcome("transient", f"request failed: {type(exc).__name__}")

    status = response.status_code
    if 200 <= status < 300:
        return _Outcome("done", f"HTTP {status}", status)
    if status >= 500 or status in _RETRYABLE_STATUSES:
        return _Outcome("transient", f"HTTP {status}", status)
    return _Outcome("permanent", f"HTTP {status}", status)


def _is_header_safe(value: str) -> bool:
    return value.isascii() and value.isprintable()


async def _load_source(db: DatabaseConnection, row: Mapping[str, Any]) -> _Source | None:
    """The message's raw source from the mirror, or from the glacier when it
    has moved there since the rule fired. None when it is in neither."""
    msg_key: str = row["msg_key"]
    lookups: list[tuple[str, str, dict[str, Any]]] = []
    if row["message_id"] is not None:
        lookups.append(("messages", "id = :id", {"id": row["message_id"]}))
    if not msg_key.startswith("sha256:"):
        key = {"account_id": row["account_id"], "hdr": msg_key}
        lookups.append(("messages", "account_id = :account_id AND message_id = :hdr", key))
        lookups.append(("glacier_messages", "account_id = :account_id AND message_id = :hdr", key))
    async with db.session() as session:
        for table, where, params in lookups:
            result = await session.execute(
                text(
                    f"SELECT raw_source, received_at, subject FROM {table} "  # noqa: S608 -- constants
                    f"WHERE {where} AND expunged_at IS NULL ORDER BY received_at DESC LIMIT 1"
                ),
                params,
            )
            found = result.one_or_none()
            if found is not None:
                raw = bytes(found.raw_source) if found.raw_source is not None else None
                return _Source(raw, found.received_at, found.subject)
    return None


async def _mark_done(
    db: DatabaseConnection, item_id: uuid.UUID, worker_id: str, http_status: int | None,
) -> None:
    """Terminal success in one statement, so the delivered time and status
    can never disagree with the row's state."""
    async with db.session() as session:
        await session.execute(
            text(
                """
                UPDATE webhook_deliveries
                SET status = 'done', http_status = :s, delivered_at = now(), last_error = NULL,
                    claimed_by = NULL, claimed_at = NULL, lease_expires_at = NULL
                WHERE id = :id AND status = 'claimed' AND claimed_by = :worker_id
                """
            ),
            {"id": item_id, "worker_id": worker_id, "s": http_status},
        )


async def _record_status(
    db: DatabaseConnection, item_id: uuid.UUID, http_status: int | None, last_error: str | None,
) -> None:
    async with db.session() as session:
        await session.execute(
            text(
                "UPDATE webhook_deliveries SET http_status = :s, "
                "last_error = COALESCE(:e, last_error) WHERE id = :id"
            ),
            {"id": item_id, "s": http_status, "e": last_error},
        )


async def _raise_failure_alert(
    db: DatabaseConnection, row: Mapping[str, Any], subject: str | None, reason: str,
    event_ring: EventRing | None, vapid_repo: VapidKeyRepository | None,
) -> None:
    from mail_verdict.alerts.dispatch import deliver_alert

    name = row["name"]
    alert = await AlertRepository(db).create_webhook_failed_alert(
        account_id=row["account_id"], message_id=row["message_id"],
        dedupe_key=f"webhook-failed:{row['id']}:{row['generation']}",
        title=f"Webhook {name!r} could not deliver {subject or '(no subject)'}",
        body=reason,
    )
    logger.warning(
        "Webhook delivery failed",
        extra={"delivery_id": str(row["id"]), "webhook": name, "reason": reason},
    )
    if alert is not None:
        await deliver_alert(db, event_ring, vapid_repo, alert)
