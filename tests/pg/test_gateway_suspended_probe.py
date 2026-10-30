"""
A breaker suspended on a missing, rejected or unpaid credential clears only
through a successful call. The gateway lets one caller per probe interval
through to make it -- against the real breaker row, because the claim on the
probe slot is a conditional UPDATE a mock cannot exercise.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from mail_verdict.core.retry import RetryConfig
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.pipeline.context import ModelGateway
from mail_verdict.pipeline.contracts import StageUnavailable
from mail_verdict.queue.circuit import CircuitBreaker, CircuitState
from mail_verdict.settings.credentials import ProviderCredentialRepository

pytestmark = pytest.mark.asyncio


async def _call(gateway: ModelGateway, category: str) -> None:
    # "custom" with no base_url fails deterministically inside the gateway,
    # past the breaker check and with no network.
    await gateway.structured_call(
        provider="custom", category=category, model="m", effort=None, max_tokens=500,
        schema_name="s", system_prompt="s", user_prompt="u", schema={},
    )


async def test_one_caller_per_interval_probes_a_suspended_breaker(
    migrated_db: DatabaseConnection,
) -> None:
    category = f"task{uuid.uuid4().hex[:8]}"
    breaker = CircuitBreaker(migrated_db, f"custom:{category}")
    await breaker.record_unavailable(reason="spent", probe_interval=timedelta(minutes=5))
    async with migrated_db.session() as session:
        await session.execute(
            text(
                "UPDATE circuit_breakers SET retry_after = now() - interval '1 second' "
                "WHERE name = :n"
            ),
            {"n": f"custom:{category}"},
        )
    gateway = ModelGateway(
        migrated_db, ProviderCredentialRepository(migrated_db, ""),
        RetryConfig(max_retries=0, base_delay=0.0, max_delay=0.0, exp_base=2.0),
    )

    with pytest.raises(StageUnavailable) as probing:
        await _call(gateway, category)
    with pytest.raises(StageUnavailable) as turned_away:
        await _call(gateway, category)

    assert "no base_url" in str(probing.value)
    assert "circuit suspended" in str(turned_away.value)
    assert (await breaker.status()).state == CircuitState.SUSPENDED
