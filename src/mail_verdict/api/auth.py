"""
API key authentication for MailVerdict.

Defense-in-depth: validates X-API-Key header on all endpoints except /health.
Auth disabled when MAIL_VERDICT_API_KEY env var is not set (dev mode).

Two enforcement mechanisms are needed because a Mount is an ASGI boundary:
FastAPI dependencies declared on a parent app never run for a mounted
sub-app's own routes. `require_auth` covers routes registered directly on
`api_router`; `ApiKeyASGIMiddleware` covers whole ASGI apps mounted beside
it (the MCP app), which have no FastAPI dependency system to hook into.
"""

from __future__ import annotations

import os
import secrets

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def is_valid_api_key(api_key: str | None) -> bool:
    """
    Check an API key against MAIL_VERDICT_API_KEY.

    Auth is disabled (always valid) when the env var is not set.

    Args:
        api_key: The key presented by the caller, if any

    Returns:
        True if auth passes (key valid or auth disabled)
    """
    expected = os.environ.get("MAIL_VERDICT_API_KEY")
    if not expected:
        return True
    return api_key is not None and secrets.compare_digest(api_key, expected)


async def require_auth(
    api_key: str | None = Security(_api_key_header),
) -> None:
    """
    Validate API key from X-API-Key header.

    Skips validation if MAIL_VERDICT_API_KEY is not set (dev mode).

    Raises:
        HTTPException: 401 if key is missing or invalid
    """
    if not is_valid_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


class ApiKeyASGIMiddleware:
    """
    Enforces the API key for an ASGI app mounted outside FastAPI's dependency system.

    Wraps a mounted sub-app (e.g. the MCP app) so `require_auth`'s Depends-based
    check, which only runs for routes on `api_router`, is not the only gate.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        api_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
        if not is_valid_api_key(api_key):
            response = JSONResponse(
                status_code=401, content={"detail": "Invalid or missing API key"},
            )
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)
