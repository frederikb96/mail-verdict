"""
GET /api/settings/providers against a real database: which provider keys
are configured, and the circuit breakers of the AI tasks -- the one read an
operator makes to see why a task is idle.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mail_verdict.api.settings_api import router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.queue.circuit import CircuitBreaker
from mail_verdict.settings.credentials import PROVIDER_ENV_VARS, ProviderCredentialRepository


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def _get(client: TestClient, migrated_db: DatabaseConnection) -> dict[str, Any]:
    with (
        patch("mail_verdict.api.settings_api.get_db_connection", return_value=migrated_db),
        patch(
            "mail_verdict.api.settings_api.get_provider_credential_repo",
            return_value=ProviderCredentialRepository(migrated_db, ""),
        ),
    ):
        resp = client.get("/settings/providers")
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _trip(db: DatabaseConnection, prefix: str) -> None:
    await CircuitBreaker(db, f"{prefix}:orders.write").record_unavailable(
        reason="credit balance exhausted", probe_interval=timedelta(minutes=5),
    )
    await CircuitBreaker(db, f"{prefix}:orders.decide").status()
    await CircuitBreaker(db, f"{prefix}:semantic").record_backoff(
        retry_after=timedelta(minutes=1), reason="rate limited",
    )
    # Not an AI task: its breaker is a queue's own and does not belong here.
    await CircuitBreaker(db, f"{prefix}:webhooks").record_unavailable(
        reason="x", probe_interval=timedelta(minutes=5),
    )


def test_each_provider_reports_configured_and_a_last_four_hint(
    client: TestClient, migrated_db: DatabaseConnection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_var in PROVIDER_ENV_VARS.values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv(PROVIDER_ENV_VARS["anthropic"], "sk-not-a-real-key-wxyz")

    providers = _get(client, migrated_db)["providers"]

    assert set(providers) == {"anthropic", "openai", "custom"}
    assert providers["anthropic"] == {"configured": True, "hint": "wxyz"}
    assert providers["openai"] == {"configured": False, "hint": None}
    assert "sk-not-a-real-key" not in str(providers)


def test_the_breakers_of_the_ai_tasks_are_listed_with_their_state(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    prefix = f"t{uuid.uuid4().hex[:8]}"
    client.portal.call(_trip, migrated_db, prefix)

    breakers = {
        b["name"]: b for b in _get(client, migrated_db)["breakers"]
        if b["name"].startswith(f"{prefix}:")
    }

    assert set(breakers) == {
        f"{prefix}:orders.write", f"{prefix}:orders.decide", f"{prefix}:semantic",
    }
    assert breakers[f"{prefix}:orders.write"]["state"] == "suspended"
    assert breakers[f"{prefix}:orders.write"]["reason"] == "credit balance exhausted"
    assert breakers[f"{prefix}:orders.write"]["retry_after"] is not None
    assert breakers[f"{prefix}:orders.decide"]["state"] == "closed"
    assert breakers[f"{prefix}:semantic"]["state"] == "open"
