"""Authentication enforcement on the /mcp mount.

A Mount is an ASGI boundary: FastAPI dependencies declared on a parent app
never run for a mounted sub-app's own routes, so `require_auth`'s Depends
check (attached per-router on `api_router`) never runs for anything mounted
beside it, including the MCP app. `/mcp` needs its own enforcement.
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

os.environ.setdefault(
    "MAIL_VERDICT_DATABASE_URL", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"
)

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
MCP_PING = {"jsonrpc": "2.0", "method": "ping", "id": 1}


def _mcp_only_app() -> FastAPI:
    """Build a minimal app mounting the MCP app the same way server.py does.

    Only the MCP app's own lifespan runs here (no database, no settings
    service) so the enforcing case can be observed reaching FastMCP itself
    rather than failing on unrelated startup dependencies.
    """
    from mail_verdict.api.auth import ApiKeyASGIMiddleware
    from mail_verdict.api.mcp_tools import mcp as mcp_server
    from mail_verdict.config import MCP_TRANSPORT

    mcp_app = mcp_server.http_app(path="/", transport=MCP_TRANSPORT)  # type: ignore[arg-type]
    app = FastAPI(lifespan=mcp_app.lifespan)
    app.mount("/mcp", ApiKeyASGIMiddleware(mcp_app))
    return app


def test_the_mcp_mount_is_wrapped_in_the_api_key_middleware() -> None:
    """The production app must not mount the raw FastMCP app directly."""
    from mail_verdict.api.auth import ApiKeyASGIMiddleware
    from mail_verdict.server import create_app

    mcp_mounts = [
        route
        for route in create_app().routes
        if isinstance(route, Mount) and route.path == "/mcp"
    ]
    assert len(mcp_mounts) == 1
    assert isinstance(mcp_mounts[0].app, ApiKeyASGIMiddleware)


def test_unauthenticated_request_to_mcp_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a key configured, a request carrying none is refused before reaching FastMCP."""
    monkeypatch.setenv("MAIL_VERDICT_API_KEY", "secret-test-key")
    app = _mcp_only_app()

    with TestClient(app) as client:
        resp = client.post("/mcp/", json=MCP_PING, headers=MCP_HEADERS)

    assert resp.status_code == 401


def test_wrong_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key that doesn't match is refused the same way as no key at all."""
    monkeypatch.setenv("MAIL_VERDICT_API_KEY", "secret-test-key")
    app = _mcp_only_app()

    with TestClient(app) as client:
        resp = client.post(
            "/mcp/", json=MCP_PING, headers={**MCP_HEADERS, "X-API-Key": "wrong-key"}
        )

    assert resp.status_code == 401


def _reached_fastmcp_itself(resp: object) -> bool:
    """True if the response is FastMCP's own JSON-RPC envelope, not our auth gate's.

    A `ping` with no prior session `initialize` is a protocol-level 400 from
    FastMCP's streamable-http transport ("Missing session ID") -- distinct
    from `ApiKeyASGIMiddleware`'s 401, and proof the request cleared auth.
    """
    body = resp.json()  # type: ignore[attr-defined]
    return "jsonrpc" in body


def test_correct_key_reaches_the_mcp_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """The enforcing case: a correct key is let through to FastMCP itself."""
    monkeypatch.setenv("MAIL_VERDICT_API_KEY", "secret-test-key")
    app = _mcp_only_app()

    with TestClient(app) as client:
        resp = client.post(
            "/mcp/", json=MCP_PING, headers={**MCP_HEADERS, "X-API-Key": "secret-test-key"}
        )

    assert resp.status_code != 401
    assert _reached_fastmcp_itself(resp)


def test_auth_disabled_when_no_key_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dev mode: with no MAIL_VERDICT_API_KEY set, requests reach FastMCP unchecked."""
    monkeypatch.delenv("MAIL_VERDICT_API_KEY", raising=False)
    app = _mcp_only_app()

    with TestClient(app) as client:
        resp = client.post("/mcp/", json=MCP_PING, headers=MCP_HEADERS)

    assert resp.status_code != 401
    assert _reached_fastmcp_itself(resp)
