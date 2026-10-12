"""
The semantic_search_mail MCP tool searches through the same function as the
REST endpoint, takes a time window, and reports tiers.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastmcp import Client

from mail_verdict.api.mcp_tools import mcp
from mail_verdict.database.connection import DatabaseConnection
from tests.pg.test_message_embeddings import (
    _QUERY_VECTOR,
    _SPREAD,
    _seed_scored_pool,
    _spread_dates,
    _unique_model,
)


@pytest_asyncio.fixture()
async def mcp_client(migrated_db: DatabaseConnection) -> AsyncIterator[Client]:
    config = SimpleNamespace(search=SimpleNamespace(exact_scan_max_rows=5000))
    patchers = [
        patch("mail_verdict.api.mcp_tools.get_db_connection", return_value=migrated_db),
        patch("mail_verdict.embeddings.service.get_config", return_value=config),
    ]
    for p in patchers:
        p.start()
    try:
        async with Client(mcp) as client:
            yield client
    finally:
        for p in patchers:
            p.stop()


def _embedding_as(model: str):  # type: ignore[no-untyped-def]
    async def embed(text: str) -> tuple[str, list[float]]:
        return model, _QUERY_VECTOR

    return patch("mail_verdict.embeddings.service.embed_search_query", embed)


@pytest.mark.asyncio
async def test_the_tool_returns_tiers_newest_first_within_a_tier(
    mcp_client: Client, migrated_db: DatabaseConnection,
) -> None:
    model = _unique_model()
    account_id, ids = await _seed_scored_pool(migrated_db, model, _SPREAD, _spread_dates())

    with _embedding_as(model):
        result = await mcp_client.call_tool(
            "semantic_search_mail", {"query": "anything", "account_id": str(account_id)},
        )

    hits = result.data
    assert hits
    assert {hit["tier"] for hit in hits} <= {1, 2}
    assert [hit["tier"] for hit in hits] == sorted(hit["tier"] for hit in hits)
    tier_one = [hit["received_at"] for hit in hits if hit["tier"] == 1]
    assert tier_one == sorted(tier_one, reverse=True)
    assert {uuid.UUID(hit["id"]) for hit in hits} <= set(ids)


@pytest.mark.asyncio
async def test_the_tool_takes_a_time_window(
    mcp_client: Client, migrated_db: DatabaseConnection,
) -> None:
    model = _unique_model()
    account_id, ids = await _seed_scored_pool(migrated_db, model, _SPREAD, _spread_dates())

    with _embedding_as(model):
        result = await mcp_client.call_tool(
            "semantic_search_mail",
            {
                "query": "anything", "account_id": str(account_id),
                "received_after": "2026-01-11T00:00:00+00:00",
                "received_before": "2026-01-21T00:00:00",  # no offset: read as UTC
            },
        )

    hits = result.data
    assert hits
    assert {uuid.UUID(hit["id"]) for hit in hits} <= set(ids[10:21])
    assert all(
        datetime(2026, 1, 11, tzinfo=timezone.utc)
        <= datetime.fromisoformat(hit["received_at"])
        <= datetime(2026, 1, 21, tzinfo=timezone.utc)
        for hit in hits
    )
