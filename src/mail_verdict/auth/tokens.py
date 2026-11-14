"""
Validating the JWT a request carries.

One validator for every kind of caller: the ID token an authenticating proxy
forwards for a browser, and the access token a native app or an agent sends
itself. A token passes when it is signed by a key the issuer publishes (an
asymmetric one -- a symmetric key in the set is ignored), names the configured
issuer, has not expired, its audience shares at least one entry with
auth.audiences, and it was issued (iat) for no longer than
auth.max_token_lifetime_seconds.

The key set is fetched from auth.jwks_url and reused for
auth.jwks_cache_seconds; a token naming a key id the set lacks triggers one
earlier refetch (at most every _REFETCH_MIN_GAP_SECONDS), which is how a
key rotation at the issuer is picked up. Validated claims are remembered by
the token's digest for auth.token_cache_seconds, never past the token's own
expiry.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

import httpx
import jwt

from mail_verdict.auth.principal import AuthError
from mail_verdict.config.loader import AuthConfig

logger = logging.getLogger(__name__)

# The key set is fetched at most this often, whatever asks for it: a token
# with a made-up key id, or an issuer that is down. Otherwise a stream of
# such requests turns into a stream of requests to the issuer.
_REFETCH_MIN_GAP_SECONDS = 30.0

# Validated tokens remembered at once. One per active session or device is
# the normal case; past this the least recently used is dropped.
_TOKEN_CACHE_SIZE = 1024


class InvalidToken(AuthError):
    """The token is missing, malformed, expired, or not ours."""


class TokenVerifier:
    """Validates bearer tokens against one issuer's published keys."""

    def __init__(self, config: AuthConfig) -> None:
        self._issuer = config.issuer
        self._jwks_url = config.jwks_url
        self._audiences = list(config.audiences)
        self._leeway = config.leeway_seconds
        self._max_lifetime = config.max_token_lifetime_seconds
        self._jwks_cache_seconds = config.jwks_cache_seconds
        self._timeout = config.request_timeout_seconds
        self._token_cache_seconds = config.token_cache_seconds
        self._keys: dict[str, jwt.PyJWK] = {}
        self._keys_fetched_at: float | None = None
        self._last_attempt = float("-inf")
        self._lock = asyncio.Lock()
        self._validated: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    async def verify(self, token: str) -> Mapping[str, Any]:
        """
        The claims of a valid token.

        Raises:
            InvalidToken: The token does not pass, for whatever reason. The
                reason is logged at debug level and never returned to the
                client.
        """
        digest = hashlib.sha256(token.encode()).hexdigest()
        cached = self._validated.get(digest)
        now = time.time()
        if cached is not None and cached[0] > now:
            self._validated.move_to_end(digest)
            return cached[1]

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            logger.debug("Rejected a token with an unreadable header: %s", exc)
            raise InvalidToken() from exc
        key = await self._key_for(header.get("kid"))
        if key is None:
            logger.debug("Rejected a token signed by an unknown key %r", header.get("kid"))
            raise InvalidToken()

        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=[key.algorithm_name],
                audience=self._audiences,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            )
        except jwt.PyJWTError as exc:
            logger.debug("Rejected a token: %s", exc)
            raise InvalidToken() from exc
        lifetime = float(claims["exp"]) - float(claims["iat"])
        if lifetime > self._max_lifetime:
            logger.info(
                "Rejected a token issued for %.0f s, longer than "
                "auth.max_token_lifetime_seconds (%.0f s)", lifetime, self._max_lifetime,
            )
            raise InvalidToken()

        expires_at = min(float(claims["exp"]), now + self._token_cache_seconds)
        self._validated[digest] = (expires_at, claims)
        if len(self._validated) > _TOKEN_CACHE_SIZE:
            self._validated.popitem(last=False)
        return claims

    async def _key_for(self, kid: Any) -> jwt.PyJWK | None:
        """The published key a token's header names. Without a key id, the
        only key in a one-key set."""
        now = time.monotonic()
        stale = (
            self._keys_fetched_at is None
            or now - self._keys_fetched_at > self._jwks_cache_seconds
        )
        wanted = stale or (kid is not None and str(kid) not in self._keys)
        if wanted and now - self._last_attempt >= _REFETCH_MIN_GAP_SECONDS:
            async with self._lock:
                # Another request may have fetched while this one waited.
                if self._last_attempt < now:
                    self._last_attempt = time.monotonic()
                    await self._fetch_keys()
        if kid is None:
            return next(iter(self._keys.values())) if len(self._keys) == 1 else None
        return self._keys.get(str(kid))

    async def _fetch_keys(self) -> None:
        """Fetch the issuer's key set. A failed fetch keeps the keys already
        held, so an issuer outage does not sign everyone out."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(self._jwks_url)
                response.raise_for_status()
                document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Fetching the issuer's keys from %s failed: %s", self._jwks_url, exc)
            return

        keys: dict[str, jwt.PyJWK] = {}
        for entry in document.get("keys", []):
            if not isinstance(entry, dict) or entry.get("kty") == "oct":
                continue
            if entry.get("use") not in (None, "sig"):
                continue
            try:
                key = jwt.PyJWK(entry)
            except jwt.PyJWTError as exc:
                logger.debug("Skipped an unusable published key: %s", exc)
                continue
            keys[str(entry.get("kid", ""))] = key
        self._keys = keys
        self._keys_fetched_at = time.monotonic()
