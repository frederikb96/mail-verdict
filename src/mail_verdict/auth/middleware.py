"""
The authentication gate in front of /api and /mcp.

A pure ASGI middleware, outermost on the application, so the principal it
resolves is bound before any router, mount or endpoint runs, and in the same
task that runs them. Every path under /api and /mcp needs a valid bearer
token except PUBLIC_PATHS; every other path is the static web UI, which is
public code and carries no data. A request without a valid token gets 401
with `WWW-Authenticate: Bearer`; a disabled or (with auto-provisioning off)
unknown user gets 403 naming which.

CORS preflights (OPTIONS) pass unauthenticated: a browser sends them without
credentials by design, and they reach no endpoint.
"""

from __future__ import annotations

import json
import logging

from starlette.types import ASGIApp, Receive, Scope, Send

from mail_verdict.auth.principal import AuthError, PrincipalResolver, bind_principal
from mail_verdict.auth.tokens import InvalidToken, TokenVerifier

logger = logging.getLogger(__name__)

# Answered without a token. Exact paths, never prefixes: readiness (the
# kubelet has no token), the sign-in discovery a native app reads before it
# has one, and the bare /mcp redirect to the mounted /mcp/.
PUBLIC_PATHS = frozenset({"/api/health", "/api/auth/config", "/mcp"})

_PROTECTED_PREFIXES = ("/api", "/mcp")


def requires_token(path: str) -> bool:
    """Whether a request path needs a valid bearer token."""
    if path in PUBLIC_PATHS:
        return False
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _PROTECTED_PREFIXES)


class AuthMiddleware:
    """Validates the bearer token and binds the principal for the request."""

    def __init__(self, app: ASGIApp, verifier: TokenVerifier, resolver: PrincipalResolver) -> None:
        self.app = app
        self.verifier = verifier
        self.resolver = resolver

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return
        if not requires_token(scope["path"]):
            await self.app(scope, receive, send)
            return

        try:
            token = _bearer_token(scope)
            if token is None:
                raise InvalidToken()
            claims = await self.verifier.verify(token)
            principal = await self.resolver.resolve(claims)
        except AuthError as exc:
            await _refuse(send, exc)
            return

        with bind_principal(principal):
            await self.app(scope, receive, send)


def _bearer_token(scope: Scope) -> str | None:
    """The token of an `Authorization: Bearer ...` header, if there is one."""
    for name, value in scope["headers"]:
        if name == b"authorization":
            header: str = bytes(value).decode("latin-1")
            scheme, _, token = header.partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
            return None
    return None


async def _refuse(send: Send, error: AuthError) -> None:
    """Answer 401 or 403 with the error's detail."""
    body = json.dumps({"detail": error.detail}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if error.status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": error.status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
