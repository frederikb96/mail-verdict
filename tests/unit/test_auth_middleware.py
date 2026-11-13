"""The gate in front of /api and /mcp, and the principal it hands on.

The MCP case is the subtle one: an MCP session runs its tools in a task the
session's first request started, so a principal read from the request-scoped
context variable there would be whoever opened the session. A tool must see
the caller of the request that carried the call.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from mail_verdict.auth.middleware import AuthMiddleware, requires_token
from mail_verdict.auth.principal import (
    PrincipalResolver,
    _DirectoryEntry,
    current_principal,
)
from mail_verdict.auth.tokens import InvalidToken
from tests.helpers.mcp_http import McpHttpSession

ALICE, BOB, CAROL = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


class _Verifier:
    """Accepts a token named after a known subject; refuses anything else."""

    async def verify(self, token: str) -> Mapping[str, Any]:
        if token not in {"alice", "bob", "carol"}:
            raise InvalidToken()
        return {"sub": token}


class _Directory:
    entries = {
        "alice": _DirectoryEntry(user_id=ALICE, role="user", disabled=False),
        "bob": _DirectoryEntry(user_id=BOB, role="user", disabled=False),
        "carol": _DirectoryEntry(user_id=CAROL, role="user", disabled=True),
    }

    async def find(self, subject: str) -> _DirectoryEntry | None:
        return self.entries.get(subject)

    async def provision(self, subject: str, email: str | None, name: str) -> _DirectoryEntry:
        raise AssertionError("not reached")

    async def touch(self, user_id: uuid.UUID, email: str | None) -> None:
        pass


def _whoami(request: Request) -> JSONResponse:
    principal = current_principal()
    return JSONResponse({"user_id": str(principal.user_id) if principal else None})


@pytest.fixture()
def client() -> Iterator[TestClient]:
    mcp = FastMCP("probe")

    @mcp.tool
    def whoami() -> str:
        """The user the call acts as."""
        principal = current_principal()
        return str(principal.user_id) if principal else "nobody"

    mcp_app = mcp.http_app(path="/")

    @asynccontextmanager
    async def lifespan(app: Starlette) -> Any:
        async with mcp_app.lifespan(app):
            yield

    inner = Starlette(
        routes=[
            Route("/api/health", _whoami),
            Route("/api/thing", _whoami, methods=["GET", "OPTIONS"]),
            Route("/ui-page", _whoami),
            Mount("/mcp", mcp_app),
        ],
        lifespan=lifespan,
    )
    resolver = PrincipalResolver(
        _Directory(), ["owner"], auto_provision=False, cache_seconds=30,
    )
    app = AuthMiddleware(inner, verifier=_Verifier(), resolver=resolver)  # type: ignore[arg-type]
    with TestClient(app) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "path",
    ["/api", "/api/", "/api/accounts", "/api/events", "/api/openapi.json", "/mcp/", "/mcp/x"],
)
def test_every_api_and_mcp_path_needs_a_token(path: str) -> None:
    assert requires_token(path)


@pytest.mark.parametrize(
    "path", ["/api/health", "/api/auth/config", "/mcp", "/", "/inbox", "/_next/x.js", "/apix"],
)
def test_readiness_sign_in_discovery_the_mcp_redirect_and_the_ui_do_not(path: str) -> None:
    assert not requires_token(path)


def test_a_request_without_a_token_is_401_with_a_bearer_challenge(client: TestClient) -> None:
    response = client.get("/api/thing")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "not_authenticated"}


@pytest.mark.parametrize("header", ["Bearer nonsense", "Basic YWxpY2U6eA==", "Bearer", "alice"])
def test_a_bad_or_foreign_credential_is_401(client: TestClient, header: str) -> None:
    assert client.get("/api/thing", headers={"Authorization": header}).status_code == 401


def test_a_disabled_user_is_403_naming_why(client: TestClient) -> None:
    response = client.get("/api/thing", headers=_bearer("carol"))

    assert response.status_code == 403
    assert response.json() == {"detail": "user_disabled"}


def test_a_valid_token_reaches_the_endpoint_as_its_user(client: TestClient) -> None:
    response = client.get("/api/thing", headers=_bearer("alice"))

    assert response.status_code == 200
    assert response.json() == {"user_id": str(ALICE)}


def test_a_public_path_answers_without_a_token(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"user_id": None}
    assert client.get("/ui-page").status_code == 200


def test_a_cors_preflight_passes_without_a_token(client: TestClient) -> None:
    assert client.options("/api/thing").status_code != 401


def test_an_mcp_tool_sees_the_caller_of_each_call_not_the_sessions_opener(
    client: TestClient,
) -> None:
    session = McpHttpSession(client, "/mcp/", _bearer("alice"))

    as_alice = session.call_tool("whoami", {}, _bearer("alice"))
    as_bob = session.call_tool("whoami", {}, _bearer("bob"))

    assert as_alice["result"]["content"][0]["text"] == str(ALICE)
    assert as_bob["result"]["content"][0]["text"] == str(BOB)


def test_an_mcp_call_without_a_token_never_reaches_the_tool(client: TestClient) -> None:
    session = McpHttpSession(client, "/mcp/", _bearer("alice"))

    response = client.post(
        "/mcp/", json={"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                       "params": {"name": "whoami", "arguments": {}}},
        headers={"mcp-session-id": session.session_id,
                 "accept": "application/json, text/event-stream"},
    )

    assert response.status_code == 401
