"""Which bearer tokens the validator accepts.

Every refusal case here is a token a real attacker or a misconfigured client
can produce; each must fail as InvalidToken, never pass and never raise
anything else (anything else would be a 500 instead of a 401).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from mail_verdict.auth.tokens import InvalidToken, TokenVerifier
from mail_verdict.config.loader import AuthConfig
from tests.setup.auth import AUDIENCE, TestIssuer


def _verifier(issuer: TestIssuer, **overrides: object) -> TokenVerifier:
    values: dict[str, object] = {
        "issuer": issuer.issuer,
        "jwks_url": issuer.jwks_url,
        "audiences": ["web-client", AUDIENCE],
        "owner_subjects": ["owner"],
        "auto_provision": True,
        "leeway_seconds": 0,
        "max_token_lifetime_seconds": 86400,
        "jwks_cache_seconds": 3600,
        "request_timeout_seconds": 5,
        "token_cache_seconds": 60,
        "principal_cache_seconds": 30,
        "native_sign_in_handle": "zitadel_jti",
        "native_client_id": "",
        "native_scopes": "openid",
        "native_redirect_uri": "app://cb",
    }
    values.update(overrides)
    return TokenVerifier(AuthConfig.model_validate(values))


@pytest.mark.asyncio
async def test_a_token_from_the_issuer_for_one_of_the_audiences_passes(
    auth_issuer: TestIssuer,
) -> None:
    claims = await _verifier(auth_issuer).verify(
        auth_issuer.mint("alice", email="alice@example.com"),
    )

    assert claims["sub"] == "alice"
    assert claims["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_any_one_audience_of_the_configured_set_is_enough(
    auth_issuer: TestIssuer,
) -> None:
    claims = await _verifier(auth_issuer).verify(auth_issuer.mint("bob", audience="web-client"))

    assert claims["sub"] == "bob"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "other_audience", "other_issuer", "expired", "foreign_key", "garbage", "unsigned",
        "no_issued_at", "lifetime_over_ceiling",
    ],
)
async def test_a_token_that_is_not_ours_is_refused(auth_issuer: TestIssuer, case: str) -> None:
    if case == "other_audience":
        token = auth_issuer.mint("mallory", audience="someone-elses-app")
    elif case == "other_issuer":
        token = auth_issuer.mint("mallory", issuer="https://elsewhere.example")
    elif case == "expired":
        token = auth_issuer.mint("mallory", expires_in=-120)
    elif case == "foreign_key":
        # Same key id, different key: a forger who read the published set.
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = auth_issuer.mint("mallory", signing_key=other)
    elif case == "no_issued_at":
        token = auth_issuer.mint("mallory", without=("iat",))
    elif case == "lifetime_over_ceiling":
        # Issued an hour ago for a day and an hour: unexpired, but longer
        # lived than any token may be.
        issued = int(time.time()) - 3600
        token = auth_issuer.mint("mallory", extra={"iat": issued, "exp": issued + 86401})
    elif case == "garbage":
        token = "not.a.jwt"
    else:
        token = jwt.encode(
            {"sub": "mallory", "iss": auth_issuer.issuer, "aud": AUDIENCE,
             "exp": int(time.time()) + 60},
            key=None, algorithm="none", headers={"kid": auth_issuer.kid},
        )

    with pytest.raises(InvalidToken):
        await _verifier(auth_issuer).verify(token)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@pytest.mark.asyncio
async def test_a_token_signed_with_the_public_key_as_an_hmac_secret_is_refused(
    auth_issuer: TestIssuer,
) -> None:
    """The classic algorithm confusion: the public key is public, so a
    validator that let the token choose HS256 would accept anything signed
    with it. PyJWT will not produce such a token, so it is built by hand."""
    public_jwk = httpx.get(auth_issuer.jwks_url).json()["keys"][0]
    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(public_jwk))
    secret = public_key.public_bytes(  # type: ignore[union-attr]
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    header = {"alg": "HS256", "typ": "JWT", "kid": auth_issuer.kid}
    payload = {
        "sub": "mallory", "iss": auth_issuer.issuer, "aud": AUDIENCE,
        "exp": int(time.time()) + 60,
    }
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()

    with pytest.raises(InvalidToken):
        await _verifier(auth_issuer).verify(f"{signing_input}.{_b64(signature)}")


@pytest.mark.asyncio
async def test_an_unreachable_issuer_refuses_rather_than_raising(
    auth_issuer: TestIssuer,
) -> None:
    verifier = _verifier(auth_issuer, jwks_url="http://127.0.0.1:1/jwks")

    with pytest.raises(InvalidToken):
        await verifier.verify(auth_issuer.mint("alice"))


@pytest.mark.asyncio
async def test_a_token_exactly_as_long_lived_as_the_ceiling_passes(
    auth_issuer: TestIssuer,
) -> None:
    """The ceiling is inclusive, so a deployment can name the exact lifetime
    of the tokens it still accepts."""
    issued = int(time.time()) - 60
    token = auth_issuer.mint("alice", extra={"iat": issued, "exp": issued + 86400})

    claims = await _verifier(auth_issuer).verify(token)

    assert claims["sub"] == "alice"
