"""
GZip response compression on the composed application.

Ordinary JSON responses compress when a client advertises gzip support;
the SSE stream (`GET /api/events`, its own `text/event-stream` media type)
never does, since a buffered or compressed live stream defeats its whole
purpose -- an event would sit in the compressor until enough of them
accumulated to flush, rather than reaching the client the moment it's
appended to the ring.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import Message, Receive, Scope, Send

os.environ.setdefault(
    "MAIL_VERDICT_DATABASE_URL", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"
)


@pytest.fixture()
def client() -> TestClient:
    """The real, fully composed application -- no lifespan (no DB needed):
    /api/events answers its own 503 "not ready" placeholder without a live
    EventRing, and /api/openapi.json needs no database at all."""
    import mail_verdict.server as server_module

    return TestClient(server_module.create_app())


def test_sse_is_never_compressed_even_when_the_client_asks_for_it(client: TestClient) -> None:
    resp = client.get("/api/events", headers={"Accept-Encoding": "gzip"})
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in resp.headers
    # The 503 placeholder line, read back exactly -- proof nothing buffered
    # or transcoded it, not just that no Content-Encoding header was set.
    assert resp.text == ": server not ready\n\n"


async def _sse_app_with_two_chunks(scope: Scope, receive: Receive, send: Send) -> None:
    """A minimal ASGI app standing in for the real SSE endpoint: one
    `text/event-stream` response sent as two separate body messages, the
    second one over GZipMiddleware's own minimum_size -- large enough that
    a middleware failing to exclude this content type would compress (and
    therefore buffer) it rather than passing it straight through."""
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"text/event-stream; charset=utf-8")],
    })
    await send({"type": "http.response.body", "body": b"id: 1\n\n", "more_body": True})
    await send({
        "type": "http.response.body",
        "body": f"data: {'x' * 2000}\n\n".encode(),
        "more_body": False,
    })


def test_gzip_middleware_passes_each_sse_chunk_through_unmodified() -> None:
    """The 503 placeholder above is a handful of bytes -- well under
    GZipMiddleware's own minimum_size, so it would go out uncompressed
    regardless of whether text/event-stream were excluded at all. Driving
    the middleware directly against two separate body messages, one of
    them over that floor, is what actually exercises the exclusion: a
    real event large enough to trip compression must still arrive as its
    own message, unmodified, not merged into one blob released only once
    the (never-ending, for a real SSE stream) response finishes."""
    middleware = GZipMiddleware(_sse_app_with_two_chunks)
    sent: list[Message] = []

    async def _send(message: Message) -> None:
        sent.append(message)

    async def _receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope: Scope = {"type": "http", "method": "GET", "headers": [(b"accept-encoding", b"gzip")]}
    asyncio.run(middleware(scope, _receive, _send))

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert "content-encoding" not in Headers(raw=start["headers"])

    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert len(bodies) == 2
    assert bodies[0]["body"] == b"id: 1\n\n"
    assert bodies[1]["body"] == f"data: {'x' * 2000}\n\n".encode()


def test_an_ordinary_json_response_compresses_when_the_client_accepts_it(
    client: TestClient,
) -> None:
    compressed = client.get("/api/openapi.json", headers={"Accept-Encoding": "gzip"})
    uncompressed = client.get("/api/openapi.json", headers={"Accept-Encoding": "identity"})

    assert compressed.headers.get("content-encoding") == "gzip"
    assert "content-encoding" not in uncompressed.headers
    # httpx decodes a gzip body transparently -- both must still carry the
    # same document.
    assert compressed.json() == uncompressed.json()
