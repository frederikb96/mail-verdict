"""
A token issuer for tests and the development stack.

The application validates every request's JWT against an issuer's published
keys and has no "auth off" mode, so anything that runs it needs an issuer.
TestIssuer is one: an RSA key generated per process, its public half served
as a JWKS document on a loopback port (a ThreadingHTTPServer on its own
thread, the shape of server.py's liveness listener), and `mint()` to sign a
token for any subject.

The `auth_issuer` fixture (registered by the root conftest) starts one per
test session and points the application's auth config at it through the
environment, with OWNER_SUBJECT as the instance owner -- so every layer's
app builds and every test can mint the token it needs. inject_bearer() wraps
an app so requests without a token act as a chosen user: what
scripts/devstack.py and the browser layer serve, since a browser cannot add a
header to every request it makes.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.types import ASGIApp, Receive, Scope, Send

# The instance owner's subject in every test session and dev stack.
OWNER_SUBJECT = "owner-subject"
# The audience every minted token carries unless told otherwise, and the one
# the application is configured to accept.
AUDIENCE = "mail-verdict-test"


class TestIssuer:
    """Signs tokens and serves the key set that verifies them."""

    __test__ = False  # not a test class, despite the name

    def __init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = uuid.uuid4().hex
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key()))
        jwks = json.dumps(
            {"keys": [{**public, "kid": self.kid, "use": "sig", "alg": "RS256"}]}
        ).encode()

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(jwks)))
                self.end_headers()
                self.wfile.write(jwks)

            def log_message(self, log_format: str, *args: Any) -> None:
                """Silence the default per-request stderr log."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="test-issuer",
        )
        self._thread.start()
        port = self._server.server_address[1]
        self.issuer = f"http://127.0.0.1:{port}"
        self.jwks_url = f"{self.issuer}/jwks"

    def mint(
        self,
        subject: str,
        *,
        email: str | None = None,
        name: str | None = None,
        audience: str = AUDIENCE,
        issuer: str | None = None,
        expires_in: float = 3600,
        signing_key: rsa.RSAPrivateKey | None = None,
    ) -> str:
        """A signed token for `subject`. Every argument past it exists to
        make a token the application must refuse."""
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": issuer or self.issuer,
            "sub": subject,
            "aud": audience,
            "iat": now,
            "exp": now + int(expires_in),
        }
        if email is not None:
            claims["email"] = email
        if name is not None:
            claims["name"] = name
        return jwt.encode(
            claims, signing_key or self._key, algorithm="RS256", headers={"kid": self.kid},
        )

    def environment(self, owner_subjects: list[str] | None = None) -> dict[str, str]:
        """The environment variables pointing the application at this issuer."""
        return {
            "MAIL_VERDICT_AUTH_ISSUER": self.issuer,
            "MAIL_VERDICT_AUTH_JWKS_URL": self.jwks_url,
            "MAIL_VERDICT_AUTH_AUDIENCES": AUDIENCE,
            "MAIL_VERDICT_AUTH_OWNER_SUBJECTS": ",".join(owner_subjects or [OWNER_SUBJECT]),
        }

    def close(self) -> None:
        """Stop serving the key set."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=1.0)


@pytest.fixture(scope="session", autouse=True)
def auth_issuer() -> Iterator[TestIssuer]:
    """The session's issuer, already configured as the application's."""
    issuer = TestIssuer()
    previous = {name: os.environ.get(name) for name in issuer.environment()}
    os.environ.update(issuer.environment())
    try:
        yield issuer
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        issuer.close()


def bearer(token: str) -> dict[str, str]:
    """The request headers carrying `token`."""
    return {"Authorization": f"Bearer {token}"}


def inject_bearer(app: ASGIApp, token: str) -> ASGIApp:
    """`app`, with every HTTP request that carries no Authorization header
    given `Bearer <token>`. A request that brings its own keeps it, so a
    client can still act as someone else."""

    async def _app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not any(
            name == b"authorization" for name, _ in scope["headers"]
        ):
            scope = {
                **scope,
                "headers": [*scope["headers"], (b"authorization", f"Bearer {token}".encode())],
            }
        await app(scope, receive, send)

    return _app
