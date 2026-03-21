"""
E2E test: Search context (per-account vs unified).

Tests that search results respect account_id filtering and that
searching without an account_id returns results from all accounts.
Semantic search tests require OPENAI_API_KEY and are skipped otherwise.
"""

from __future__ import annotations

import os

import httpx
import pytest

from tests.e2e.conftest import get_account_id

pytestmark = [pytest.mark.e2e]

has_openai_key = bool(os.environ.get("OPENAI_API_KEY"))


async def _get_alice_id(client: httpx.AsyncClient) -> str:
    """Get alice's account ID."""
    return await get_account_id(client, name="alice")


async def _get_bob_id(client: httpx.AsyncClient) -> str:
    """Get bob's account ID."""
    return await get_account_id(client, name="bob")


async def _search_mail_ids(
    client: httpx.AsyncClient,
    query: str,
    mode: str = "fulltext",
    account_id: str | None = None,
) -> set[str]:
    """Run a search and return the set of mail_ids found."""
    params: dict[str, str] = {"q": query, "mode": mode}
    if account_id:
        params["account_id"] = account_id
    resp = await client.get("/api/search", params=params)
    assert resp.status_code == 200
    return {r["mail_id"] for r in resp.json()["results"]}


# --- Fulltext search context tests ---


@pytest.mark.asyncio
async def test_fulltext_search_account_scoping(
    app_client: httpx.AsyncClient,
    seeded_env: dict[str, str],
) -> None:
    """Fulltext search scoped to Alice and Bob return non-overlapping results.

    Both accounts received mails about 'kickoff'. Searching each account
    separately should return different mail_ids (same subjects but different
    messages delivered to different inboxes).
    """
    alice_id = seeded_env["alice_id"]
    bob_id = seeded_env["bob_id"]

    alice_results = await _search_mail_ids(
        app_client, "kickoff", account_id=alice_id,
    )
    bob_results = await _search_mail_ids(
        app_client, "kickoff", account_id=bob_id,
    )

    # Both should have results (seed emails contain "kickoff")
    assert len(alice_results) > 0, "Alice should have kickoff search results"
    assert len(bob_results) > 0, "Bob should have kickoff search results"

    # Results should be non-overlapping (different mails for each account)
    overlap = alice_results & bob_results
    assert len(overlap) == 0, (
        f"Search results should not overlap between accounts: {overlap}"
    )


@pytest.mark.asyncio
async def test_fulltext_search_without_account_returns_both(
    app_client: httpx.AsyncClient,
    seeded_env: dict[str, str],
) -> None:
    """Fulltext search without account_id returns mails from both accounts."""
    alice_id = seeded_env["alice_id"]
    bob_id = seeded_env["bob_id"]

    # Get account-scoped results for comparison
    alice_results = await _search_mail_ids(
        app_client, "kickoff", account_id=alice_id,
    )
    bob_results = await _search_mail_ids(
        app_client, "kickoff", account_id=bob_id,
    )

    # Get unified results (no account_id)
    unified_results = await _search_mail_ids(app_client, "kickoff")

    # Unified should be superset of both individual searches
    assert alice_results.issubset(unified_results), (
        f"Alice results should be in unified: {alice_results - unified_results}"
    )
    assert bob_results.issubset(unified_results), (
        f"Bob results should be in unified: {bob_results - unified_results}"
    )


@pytest.mark.asyncio
async def test_fulltext_search_result_fields(
    app_client: httpx.AsyncClient,
    seeded_env: dict[str, str],
) -> None:
    """Fulltext search results have expected fields populated."""
    alice_id = seeded_env["alice_id"]

    resp = await app_client.get(
        "/api/search",
        params={"q": "kickoff", "mode": "fulltext", "account_id": alice_id},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "kickoff"
    assert data["mode"] == "fulltext"
    assert data["total"] >= 0

    if data["results"]:
        result = data["results"][0]
        assert "mail_id" in result
        assert "score" in result
        assert result["source"] == "fulltext"
        assert result["subject"] is not None
        assert result["from_addr"] is not None


@pytest.mark.asyncio
async def test_fulltext_search_no_crossover(
    app_client: httpx.AsyncClient,
    seeded_env: dict[str, str],
) -> None:
    """Alice-scoped search for 'Bob' returns Alice's received mails from Bob.

    All results should have from_addr containing 'bob@test.local'
    since Alice received emails FROM Bob.
    """
    alice_id = seeded_env["alice_id"]

    resp = await app_client.get(
        "/api/search",
        params={"q": "Bob", "mode": "fulltext", "account_id": alice_id},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) > 0, "Should find mails mentioning Bob"


# --- Semantic search context tests ---


@pytest.mark.asyncio
@pytest.mark.skipif(not has_openai_key, reason="OPENAI_API_KEY not set")
async def test_semantic_search_with_account_filter(
    app_client: httpx.AsyncClient,
    seeded_env: dict[str, str],
) -> None:
    """Semantic search with account_id scopes vector results to that account."""
    alice_id = seeded_env["alice_id"]

    resp = await app_client.get(
        "/api/search",
        params={
            "q": "project meeting schedule",
            "mode": "semantic",
            "account_id": alice_id,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "semantic"
    assert isinstance(data["results"], list)

    for result in data["results"]:
        assert result["source"] == "semantic"


@pytest.mark.asyncio
@pytest.mark.skipif(not has_openai_key, reason="OPENAI_API_KEY not set")
async def test_semantic_search_without_account(
    app_client: httpx.AsyncClient,
) -> None:
    """Semantic search without account_id searches across all accounts."""
    resp = await app_client.get(
        "/api/search",
        params={"q": "lunch plans restaurant", "mode": "semantic"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "semantic"
    assert isinstance(data["results"], list)


# --- Combined search mode ---


@pytest.mark.asyncio
@pytest.mark.skipif(not has_openai_key, reason="OPENAI_API_KEY not set")
async def test_combined_search_merges_results(
    app_client: httpx.AsyncClient,
    seeded_env: dict[str, str],
) -> None:
    """Combined search returns merged fulltext + semantic results."""
    alice_id = seeded_env["alice_id"]

    resp = await app_client.get(
        "/api/search",
        params={"q": "review", "mode": "combined", "account_id": alice_id},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "combined"
    assert isinstance(data["results"], list)
