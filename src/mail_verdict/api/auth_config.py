"""
GET /api/auth/config -- how to sign in, for a client that holds no token yet.

The one /api route answered without a token besides readiness: a native app
reads it to start an OIDC authorization-code sign-in against this server's
issuer. It carries nothing secret -- the issuer URL and a public client id.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from mail_verdict.config import get_config

router = APIRouter(tags=["auth"])


class AuthConfigResponse(BaseModel):
    """Sign-in parameters. `client_id` is null when this server offers no
    native sign-in (auth.native_client_id unset)."""

    issuer: str
    client_id: str | None
    scopes: str
    redirect_uri: str


@router.get("/auth/config", response_model=AuthConfigResponse)
async def get_auth_config() -> AuthConfigResponse:
    """The issuer and native client a sign-in starts from."""
    auth = get_config().auth
    return AuthConfigResponse(
        issuer=auth.issuer,
        client_id=auth.native_client_id or None,
        scopes=auth.native_scopes,
        redirect_uri=auth.native_redirect_uri,
    )
