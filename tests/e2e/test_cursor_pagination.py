"""
E2E test: Cursor-based pagination.

Tests the before_id cursor pagination on /api/mails,
verifying stable ordering under multiple pages and edge cases.
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import get_account_id

pytestmark = [pytest.mark.e2e]


async def _get_alice_account_id(client: httpx.AsyncClient) -> str:
    """Find the alice account's ID."""
    return await get_account_id(client, name="alice")


@pytest.mark.asyncio
async def test_first_page_returns_mails(app_client: httpx.AsyncClient) -> None:
    """First page (no cursor) returns mails with pagination metadata."""
    account_id = await _get_alice_account_id(app_client)
    resp = await app_client.get(
        "/api/mails",
        params={"account_id": account_id, "limit": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "mails" in data
    assert "has_more" in data
    assert "next_cursor" in data
    assert isinstance(data["mails"], list)
    assert len(data["mails"]) <= 3


@pytest.mark.asyncio
async def test_cursor_pagination_traverses_all_mails(
    app_client: httpx.AsyncClient,
) -> None:
    """Paginate through all mails using cursors, collecting unique IDs."""
    account_id = await _get_alice_account_id(app_client)
    all_ids: list[str] = []
    cursor = None
    pages = 0
    max_pages = 20

    while pages < max_pages:
        params: dict = {"account_id": account_id, "limit": 5}
        if cursor:
            params["before"] = cursor
        resp = await app_client.get("/api/mails", params=params)
        assert resp.status_code == 200
        data = resp.json()
        mails = data["mails"]

        for m in mails:
            assert m["id"] not in all_ids, f"Duplicate mail ID {m['id']} across pages"
            all_ids.append(m["id"])

        if not data["has_more"]:
            break
        cursor = data["next_cursor"]
        assert cursor is not None
        pages += 1

    assert len(all_ids) > 0, "Expected at least some mails"


@pytest.mark.asyncio
async def test_cursor_maintains_sort_order(
    app_client: httpx.AsyncClient,
) -> None:
    """Mails across pages are in descending received_at order."""
    account_id = await _get_alice_account_id(app_client)
    all_dates: list[str] = []
    cursor = None
    pages = 0

    while pages < 10:
        params: dict = {"account_id": account_id, "limit": 3}
        if cursor:
            params["before"] = cursor
        resp = await app_client.get("/api/mails", params=params)
        assert resp.status_code == 200
        data = resp.json()

        for m in data["mails"]:
            if m["received_at"]:
                all_dates.append(m["received_at"])

        if not data["has_more"]:
            break
        cursor = data["next_cursor"]
        pages += 1

    # Verify descending order
    for i in range(1, len(all_dates)):
        assert all_dates[i - 1] >= all_dates[i], (
            f"Sort order violated at index {i}: {all_dates[i-1]} < {all_dates[i]}"
        )


@pytest.mark.asyncio
async def test_invalid_cursor_returns_400(
    app_client: httpx.AsyncClient,
) -> None:
    """Invalid cursor UUID returns 400."""
    account_id = await _get_alice_account_id(app_client)
    resp = await app_client.get(
        "/api/mails",
        params={
            "account_id": account_id,
            "before": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_limit_respected(app_client: httpx.AsyncClient) -> None:
    """Response never exceeds requested limit."""
    account_id = await _get_alice_account_id(app_client)
    for limit in [1, 2, 5]:
        resp = await app_client.get(
            "/api/mails",
            params={"account_id": account_id, "limit": limit},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["mails"]) <= limit


@pytest.mark.asyncio
async def test_account_filter_isolates_mails(
    app_client: httpx.AsyncClient,
) -> None:
    """Filtering by account_id returns only that account's mails."""
    account_id = await _get_alice_account_id(app_client)
    resp = await app_client.get(
        "/api/mails",
        params={"account_id": account_id, "limit": 50},
    )
    assert resp.status_code == 200
    data = resp.json()
    for m in data["mails"]:
        assert m["account_id"] == account_id, (
            f"Mail {m['id']} has account_id={m['account_id']}, expected {account_id}"
        )
