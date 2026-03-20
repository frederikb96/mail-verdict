"""
E2E test: Health, stats, and basic API infrastructure.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = [pytest.mark.e2e]


@pytest.mark.asyncio
async def test_health_endpoint(app_client: httpx.AsyncClient) -> None:
    """Health endpoint returns 200 with all dependencies OK."""
    resp = await app_client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["dependencies"]["postgres"] == "ok"
    assert data["dependencies"]["qdrant"] == "ok"


@pytest.mark.asyncio
async def test_stats_endpoint(app_client: httpx.AsyncClient) -> None:
    """Stats endpoint returns valid response."""
    resp = await app_client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_mails" in data
    assert "total_accounts" in data
    assert "accuracy" in data


@pytest.mark.asyncio
async def test_jobs_list(app_client: httpx.AsyncClient) -> None:
    """Jobs endpoint returns list (may be empty)."""
    resp = await app_client.get("/api/jobs")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_mail_list_empty_or_populated(app_client: httpx.AsyncClient) -> None:
    """Mail list endpoint responds with valid list."""
    resp = await app_client.get("/api/mails", params={"limit": 10})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_rules_list(app_client: httpx.AsyncClient) -> None:
    """Rules endpoint returns list (may be empty with no rules configured)."""
    resp = await app_client.get("/api/rules")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_verdicts_list(app_client: httpx.AsyncClient) -> None:
    """Verdicts endpoint returns list."""
    resp = await app_client.get("/api/verdicts")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
