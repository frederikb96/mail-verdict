"""
E2E test: Settings CRUD via API.

Tests reading, updating, and importing settings.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = [pytest.mark.e2e]


@pytest.mark.asyncio
async def test_get_all_settings_returns_defaults(app_client: httpx.AsyncClient) -> None:
    """GET /api/settings returns all categories with defaults."""
    resp = await app_client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "ai" in data
    assert "spam" in data
    assert "sync" in data
    assert "retry" in data
    assert data["ai"]["model"] == "gpt-5-mini"


@pytest.mark.asyncio
async def test_get_single_category(app_client: httpx.AsyncClient) -> None:
    """GET /api/settings/ai returns AI settings."""
    resp = await app_client.get("/api/settings/ai")
    assert resp.status_code == 200
    data = resp.json()
    assert "model" in data
    assert "embedding_model" in data


@pytest.mark.asyncio
async def test_update_setting(app_client: httpx.AsyncClient) -> None:
    """PUT /api/settings/retry updates and persists."""
    resp = await app_client.put("/api/settings/retry", json={"data": {"max_retries": 5}})
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["max_retries"] == 5

    # Verify persistence
    resp2 = await app_client.get("/api/settings/retry")
    assert resp2.json()["max_retries"] == 5

    # Restore default
    await app_client.put("/api/settings/retry", json={"data": {"max_retries": 3}})


@pytest.mark.asyncio
async def test_import_bulk_settings(app_client: httpx.AsyncClient) -> None:
    """POST /api/settings/import merges multiple categories."""
    resp = await app_client.post("/api/settings/import", json={
        "data": {
            "sync": {"poll_interval_seconds": 60},
            "spam": {"neighbor_count": 5},
        },
    })
    assert resp.status_code == 200

    # Verify
    sync = await app_client.get("/api/settings/sync")
    assert sync.json()["poll_interval_seconds"] == 60

    # Restore defaults
    await app_client.post("/api/settings/import", json={
        "data": {
            "sync": {"poll_interval_seconds": 300},
            "spam": {"neighbor_count": 3},
        },
    })


@pytest.mark.asyncio
async def test_invalid_category_rejected(app_client: httpx.AsyncClient) -> None:
    """Invalid category returns 400."""
    resp = await app_client.get("/api/settings/nonexistent")
    assert resp.status_code == 400
