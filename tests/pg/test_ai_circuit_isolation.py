"""
The circuit breaker each of the three AI-backed queues (pipeline, orders,
embeddings) actually trips, proven independent even when their settings
categories share one provider name -- the shape a "custom" deployment
almost always is, since ai/semantic/orders orders itself all reuse
settings.ai's provider, key and base_url.

Registration alone cannot prove this: `QueueManager.summary()` merely
reads back whatever name a queue's `circuit_name` resolver reports, so a
resolver and the breaker `ModelGateway.structured_call` actually writes
to can drift apart silently. This drives a real, failing orders decide
call through `orders/worker.py`'s own code path and checks the *other*
two queues' reported breakers never moved.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import OrderJob
from mail_verdict.database.repository import AccountPrefsRepository
from mail_verdict.embeddings.worker import register_embeddings
from mail_verdict.orders import repository as orders_repository
from mail_verdict.orders.worker import QUEUE_NAME as ORDERS_QUEUE_NAME
from mail_verdict.orders.worker import _handle_mail_job, register_orders
from mail_verdict.pipeline.contracts import StageUnavailable
from mail_verdict.pipeline.revisions import PipelineRevisionRepository, build_migrated_definition
from mail_verdict.pipeline.runner import QUEUE_NAME as PIPELINE_QUEUE_NAME
from mail_verdict.pipeline.runner import PipelineRunner
from mail_verdict.queue.circuit import CircuitState
from mail_verdict.queue.manager import QueueManager
from mail_verdict.settings.credentials import ProviderCredentialRepository
from mail_verdict.settings.service import SettingsService

pytestmark = pytest.mark.asyncio

_EMBEDDINGS_QUEUE_NAME = "embeddings"
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


async def _seed_account_and_folder(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    account_id = uuid.uuid4()
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text("INSERT INTO folders (id, account_id, imap_name) VALUES (:id, :account_id, 'INBOX')"),
        {"id": folder_id, "account_id": account_id},
    )
    await session.execute(
        text("INSERT INTO account_prefs (account_id, orders_enabled) VALUES (:id, true)"),
        {"id": account_id},
    )
    return account_id, folder_id


async def _seed_order_mail_job(
    session: AsyncSession, *, account_id: uuid.UUID, folder_id: uuid.UUID,
) -> dict[str, object]:
    mail_id = uuid.uuid4()
    header = f"<{uuid.uuid4()}@example.com>"
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, from_addr, "
            "subject, body_text, received_at, size_bytes, is_seen) "
            "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :message_id, "
            "'shop@example.com', 'Order NK-1 confirmed', 'Thanks for your order.', "
            ":received_at, 512, false)"
        ),
        {
            "id": mail_id, "account_id": account_id, "folder_id": folder_id,
            "thread_id": uuid.uuid4(), "message_id": header, "received_at": _NOW,
        },
    )
    await orders_repository.enqueue_mail_job(
        session, account_id=account_id, msg_key=header, message_id=mail_id,
        origin="live", priority=0, filter_reason="subject", next_attempt_at=_NOW,
    )
    row = (
        await session.execute(
            select(OrderJob).where(OrderJob.account_id == account_id, OrderJob.msg_key == header)
        )
    ).scalar_one()
    return {
        "id": row.id, "kind": "mail", "account_id": row.account_id, "msg_key": row.msg_key,
        "message_id": row.message_id, "origin": row.origin, "filter_reason": row.filter_reason,
    }


async def test_a_broken_orders_model_does_not_open_classifys_or_embeddings_circuit(
    migrated_db: DatabaseConnection,
) -> None:
    """settings.ai and settings.semantic both name provider "custom" --
    the same provider settings.orders' calls resolve to as well, since
    orders has no provider setting of its own. A call that only orders'
    own settings would make failing (no base_url configured, so
    ModelGateway's resolve_client fails deterministically with no
    network) must trip only the orders queue's own breaker."""
    document = build_migrated_definition(
        raw_rules=[], spam_settings={"enabled": True, "auto_move_to_junk": True},
    )
    await PipelineRevisionRepository(migrated_db).append(document, note="test baseline")

    settings_service = SettingsService(migrated_db)
    await settings_service.load()
    await settings_service.update("ai", {"provider": "custom"})
    await settings_service.update("semantic", {"provider": "custom"})

    cred_repo = ProviderCredentialRepository(migrated_db, encryption_key="")
    manager = QueueManager(migrated_db)
    PipelineRunner(
        migrated_db, settings_service, cred_repo, AccountPrefsRepository(migrated_db),
        event_ring=None,
    ).register(manager)
    register_orders(manager, migrated_db, cred_repo, settings_service, event_ring=None)
    register_embeddings(manager, migrated_db, cred_repo, settings_service)

    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        row = await _seed_order_mail_job(session, account_id=account_id, folder_id=folder_id)

    with pytest.raises(StageUnavailable):
        await _handle_mail_job(row, migrated_db, cred_repo, settings_service)

    assert (await manager.summary(ORDERS_QUEUE_NAME)).circuit.state == CircuitState.SUSPENDED
    assert (await manager.summary(PIPELINE_QUEUE_NAME)).circuit.state == CircuitState.CLOSED
    assert (await manager.summary(_EMBEDDINGS_QUEUE_NAME)).circuit.state == CircuitState.CLOSED
