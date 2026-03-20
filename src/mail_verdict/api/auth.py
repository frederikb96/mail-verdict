"""
API key authentication for MailVerdict.

Defense-in-depth: validates X-API-Key header on all endpoints except /health.
Auth disabled when MAIL_VERDICT_API_KEY env var is not set (dev mode).
"""

from __future__ import annotations

import os
import secrets

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_auth(
    api_key: str | None = Security(_api_key_header),
) -> None:
    """
    Validate API key from X-API-Key header.

    Skips validation if MAIL_VERDICT_API_KEY is not set (dev mode).

    Raises:
        HTTPException: 401 if key is missing or invalid
    """
    expected = os.environ.get("MAIL_VERDICT_API_KEY")
    if not expected:
        return
    if not api_key or not secrets.compare_digest(api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
