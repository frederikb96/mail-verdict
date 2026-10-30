"""
alerts/provider_health.py against a real database: an AI provider whose
breaker stays suspended past the threshold raises one durable alert per
outage, and the alert resolves when the outage ends.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from mail_verdict.alerts.provider_health import (
    ALERT_KIND,
    DEDUPE_PREFIX,
    raise_provider_alerts_once,
)
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Alert
from mail_verdict.queue.circuit import CircuitBreaker

pytestmark = pytest.mark.asyncio

_THRESHOLD = timedelta(minutes=30)


async def _suspend(
    db: DatabaseConnection, name: str, *, for_minutes: int,
    reason: str = "credit balance exhausted",
) -> None:
    await CircuitBreaker(db, name).record_unavailable(
        reason=reason, probe_interval=timedelta(minutes=5),
    )
    async with db.session() as session:
        await session.execute(
            text("UPDATE circuit_breakers SET since = now() - make_interval(mins => :m) "
                 "WHERE name = :n"),
            {"m": for_minutes, "n": name},
        )


async def _alerts(db: DatabaseConnection, prefix: str) -> list[Alert]:
    async with db.session() as session:
        return list(
            (
                await session.execute(
                    select(Alert).where(
                        Alert.kind == ALERT_KIND,
                        Alert.dedupe_key.like(f"{DEDUPE_PREFIX}{prefix}:%"),
                    )
                )
            ).scalars().all()
        )


async def _pass(db: DatabaseConnection) -> None:
    await raise_provider_alerts_once(db, None, None, threshold=_THRESHOLD)


async def test_a_provider_suspended_past_the_threshold_raises_one_alert_per_outage(
    migrated_db: DatabaseConnection,
) -> None:
    prefix = f"t{uuid.uuid4().hex[:8]}"
    await _suspend(migrated_db, f"{prefix}:orders.write", for_minutes=45)

    await _pass(migrated_db)
    await _pass(migrated_db)

    [alert] = await _alerts(migrated_db, prefix)
    assert alert.title == "AI provider unavailable"
    assert alert.body is not None
    assert f"{prefix}:orders.write" in alert.body and "credit balance exhausted" in alert.body
    assert alert.delivered_at is not None and alert.dismissed_at is None
    assert alert.account_id is None


async def test_a_provider_suspended_only_briefly_raises_nothing(
    migrated_db: DatabaseConnection,
) -> None:
    prefix = f"t{uuid.uuid4().hex[:8]}"
    await _suspend(migrated_db, f"{prefix}:ai", for_minutes=10)

    await _pass(migrated_db)

    assert await _alerts(migrated_db, prefix) == []


async def test_a_queue_breaker_that_is_not_an_ai_task_raises_nothing(
    migrated_db: DatabaseConnection,
) -> None:
    prefix = f"t{uuid.uuid4().hex[:8]}"
    await _suspend(migrated_db, f"{prefix}:webhooks", for_minutes=600)

    await _pass(migrated_db)

    assert await _alerts(migrated_db, prefix) == []


async def test_the_alert_resolves_when_the_provider_answers_again(
    migrated_db: DatabaseConnection,
) -> None:
    prefix = f"t{uuid.uuid4().hex[:8]}"
    name = f"{prefix}:orders.write"
    await _suspend(migrated_db, name, for_minutes=45)
    await _pass(migrated_db)
    await CircuitBreaker(migrated_db, name).record_success()

    await _pass(migrated_db)

    [alert] = await _alerts(migrated_db, prefix)
    assert alert.dismissed_at is not None


async def test_a_second_outage_of_the_same_provider_alerts_again(
    migrated_db: DatabaseConnection,
) -> None:
    prefix = f"t{uuid.uuid4().hex[:8]}"
    name = f"{prefix}:orders.write"
    await _suspend(migrated_db, name, for_minutes=45)
    await _pass(migrated_db)
    await CircuitBreaker(migrated_db, name).record_success()
    await _pass(migrated_db)

    await _suspend(migrated_db, name, for_minutes=40)
    await _pass(migrated_db)

    alerts = await _alerts(migrated_db, prefix)
    assert len(alerts) == 2
    assert sorted(a.dismissed_at is None for a in alerts) == [False, True]
