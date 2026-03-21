"""
E2E test: Search API.

Tests full-text and semantic search endpoints.
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import get_account_id

pytestmark = [pytest.mark.e2e]


async def _get_alice_id(client: httpx.AsyncClient) -> str:
    """Get alice's account ID."""
    return await get_account_id(client, name="alice")


@pytest.mark.asyncio
async def test_fulltext_search_returns_results(
    app_client: httpx.AsyncClient,
) -> None:
    """Full-text search finds mails matching the query."""
    account_id = await _get_alice_id(app_client)
    resp = await app_client.get(
        "/api/search",
        params={
            "q": "meeting",
            "mode": "fulltext",
            "account_id": account_id,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data
    assert "total" in data
    assert data["mode"] == "fulltext"
    assert data["query"] == "meeting"
    assert isinstance(data["results"], list)


@pytest.mark.asyncio
async def test_fulltext_search_without_account(
    app_client: httpx.AsyncClient,
) -> None:
    """Full-text search without account_id searches across all accounts."""
    resp = await app_client.get(
        "/api/search",
        params={"q": "test", "mode": "fulltext"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "fulltext"


@pytest.mark.asyncio
async def test_search_empty_query_rejected(
    app_client: httpx.AsyncClient,
) -> None:
    """Empty search query is rejected (min_length=1)."""
    resp = await app_client.get(
        "/api/search",
        params={"q": "", "mode": "fulltext"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_search_invalid_mode_rejected(
    app_client: httpx.AsyncClient,
) -> None:
    """Invalid search mode is rejected."""
    resp = await app_client.get(
        "/api/search",
        params={"q": "hello", "mode": "invalid_mode"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_search_result_structure(
    app_client: httpx.AsyncClient,
) -> None:
    """Search results have expected fields."""
    resp = await app_client.get(
        "/api/search",
        params={"q": "alice", "mode": "fulltext"},
    )
    assert resp.status_code == 200
    data = resp.json()
    if data["results"]:
        result = data["results"][0]
        assert "mail_id" in result
        assert "score" in result
        assert "source" in result
        assert result["source"] == "fulltext"
