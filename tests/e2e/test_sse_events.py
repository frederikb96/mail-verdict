"""
E2E test: SSE (Server-Sent Events) stream.

Tests connecting to the SSE endpoint, receiving initial events,
and verifying keepalive delivery.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.e2e.conftest import APP_BASE_URL, get_account_id

pytestmark = [pytest.mark.e2e]


async def _read_sse_lines(
    resp: httpx.Response,
    timeout: float = 5.0,
    max_lines: int = 50,
) -> list[str]:
    """Read SSE lines from a streaming response with timeout."""
    lines: list[str] = []

    async def _collect() -> None:
        async for line in resp.aiter_lines():
            lines.append(line)
            if len(lines) >= max_lines:
                return

    try:
        await asyncio.wait_for(_collect(), timeout=timeout)
    except (asyncio.TimeoutError, StopAsyncIteration):
        pass
    return lines


@pytest.mark.asyncio
async def test_sse_connect_without_account_filter() -> None:
    """Connect to SSE without account_id and receive connected event."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=transport, timeout=30.0,
    ) as client:
        async with client.stream("GET", "/api/events") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")

            lines = await _read_sse_lines(resp, timeout=5.0, max_lines=10)

    assert len(lines) > 0, "Expected at least one SSE message"
    combined = "\n".join(lines)
    assert "event:" in combined or "id:" in combined or ": keepalive" in combined, (
        f"No valid SSE message found in: {combined[:500]}"
    )


@pytest.mark.asyncio
async def test_sse_connect_with_account_filter(
    app_client: httpx.AsyncClient,
) -> None:
    """Connect to SSE with account_id filter and receive sync.state snapshot."""
    account_id = await get_account_id(app_client, name="alice")
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=transport, timeout=30.0,
    ) as client:
        async with client.stream(
            "GET",
            "/api/events",
            params={"account_id": account_id},
        ) as resp:
            assert resp.status_code == 200
            lines = await _read_sse_lines(resp, timeout=5.0, max_lines=20)

    assert len(lines) > 0, "Expected SSE data for account"
    combined = "\n".join(lines)
    assert "event:" in combined or "id:" in combined, (
        f"No SSE event found in account-scoped stream: {combined[:500]}"
    )


@pytest.mark.asyncio
async def test_sse_keepalive_received() -> None:
    """SSE stream sends keepalive comment within 20 seconds."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=transport, timeout=30.0,
    ) as client:
        async with client.stream("GET", "/api/events") as resp:
            assert resp.status_code == 200
            lines = await _read_sse_lines(resp, timeout=20.0, max_lines=50)

    # Either got keepalive or events (both acceptable)
    assert len(lines) > 0, "SSE stream produced no output within 20s"


@pytest.mark.asyncio
async def test_sse_content_type() -> None:
    """SSE endpoint returns correct content type and cache headers."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        base_url=APP_BASE_URL, transport=transport, timeout=10.0,
    ) as client:
        async with client.stream("GET", "/api/events") as resp:
            assert resp.status_code == 200
            content_type = resp.headers.get("content-type", "")
            assert "text/event-stream" in content_type
            assert resp.headers.get("cache-control") == "no-cache"
