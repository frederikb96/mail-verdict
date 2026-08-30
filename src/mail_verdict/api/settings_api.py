"""
Settings API endpoints.

GET /api/settings — all settings by category
GET /api/settings/{category} — single category
PUT /api/settings/{category} — update category (merge)
POST /api/settings/import — bulk import

The "ai" category carries provider API keys as a write-only extension:
PUT accepts anthropic_api_key / openai_api_key (plaintext, encrypted and
stored on write), and every read reports only anthropic_api_key_configured
/ anthropic_api_key_hint (and the openai equivalents) -- the key itself is
never merged into the JSONB settings blob and never appears in a response.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mail_verdict.settings import SettingCategory, get_settings_service
from mail_verdict.settings.ai_validation import validate_ai_settings
from mail_verdict.settings.credentials import (
    PROVIDER_ENV_VARS,
    EncryptionUnavailableError,
    get_provider_credential_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

_VALID_CATEGORIES = {cat.value for cat in SettingCategory}
_CREDENTIAL_FIELDS = {f"{provider}_api_key" for provider in PROVIDER_ENV_VARS}
# Read-only, computed on every GET -- stripped from any write so a client
# that round-trips a GET response back through PUT/import can't persist a
# stale status snapshot into the JSONB blob (harmless since the next GET
# overwrites it anyway, but pointless to store).
_CREDENTIAL_STATUS_FIELDS = {
    f"{field}_{suffix}"
    for field in _CREDENTIAL_FIELDS
    for suffix in ("configured", "hint")
}
_AI_COMPUTED_FIELDS = _CREDENTIAL_FIELDS | _CREDENTIAL_STATUS_FIELDS


class SettingsUpdateRequest(BaseModel):
    """Request to update settings for a category."""

    data: dict[str, Any]


class SettingsImportRequest(BaseModel):
    """Request to bulk import settings."""

    data: dict[str, dict[str, Any]]


async def _ai_credential_status() -> dict[str, Any]:
    """Report presence + a last-four hint for every provider key, never the key."""
    cred_repo = get_provider_credential_repo()
    status: dict[str, Any] = {}
    for provider in PROVIDER_ENV_VARS:
        provider_status = await cred_repo.status(provider)
        status[f"{provider}_api_key_configured"] = provider_status["configured"]
        status[f"{provider}_api_key_hint"] = provider_status["hint"]
    return status


async def _with_ai_credential_status(data: dict[str, Any]) -> dict[str, Any]:
    """Augment an ai settings dict with credential status, in place semantics."""
    return {**data, **(await _ai_credential_status())}


async def _apply_credential_writes(data: dict[str, Any]) -> dict[str, Any]:
    """
    Extract and store any provider_api_key fields from a PUT body.

    An empty string clears the stored key; anything else (over)writes it.
    Returns the request data with credential fields removed, so they are
    never merged into the JSONB settings blob.

    Args:
        data: Raw PUT body for the "ai" category

    Returns:
        data with anthropic_api_key / openai_api_key and the read-only
        computed status fields popped out

    Raises:
        HTTPException: 400 if a key is set with no ENCRYPTION_KEY configured
    """
    remaining = {k: v for k, v in data.items() if k not in _CREDENTIAL_STATUS_FIELDS}
    cred_repo = get_provider_credential_repo()
    for provider in PROVIDER_ENV_VARS:
        field_name = f"{provider}_api_key"
        if field_name not in remaining:
            continue
        value = remaining.pop(field_name)
        try:
            if value:
                await cred_repo.set_key(provider, str(value))
            else:
                await cred_repo.clear_key(provider)
        except EncryptionUnavailableError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return remaining


@router.get("")
async def get_all_settings() -> dict[str, dict[str, Any]]:
    """Get all settings grouped by category."""
    service = get_settings_service()
    all_settings = service.get_all()
    all_settings["ai"] = await _with_ai_credential_status(all_settings["ai"])
    return all_settings


@router.get("/{category}")
async def get_settings(category: str) -> dict[str, Any]:
    """Get settings for a single category."""
    if category not in _VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid category '{category}'. Valid: {sorted(_VALID_CATEGORIES)}",
        )
    service = get_settings_service()
    data = service.get(category)
    if category == "ai":
        data = await _with_ai_credential_status(data)
    return data


@router.put("/{category}")
async def update_settings(category: str, request: SettingsUpdateRequest) -> dict[str, Any]:
    """Update settings for a category (merge semantics)."""
    if category not in _VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid category '{category}'. Valid: {sorted(_VALID_CATEGORIES)}",
        )
    service = get_settings_service()
    data = request.data

    if category == "ai":
        data = await _apply_credential_writes(data)
        effective = {**service.get("ai"), **data}
        try:
            validate_ai_settings(effective)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        result = await service.update(category, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if category == "ai":
        result = await _with_ai_credential_status(result)
    return result


@router.post("/import")
async def import_settings(request: SettingsImportRequest) -> dict[str, dict[str, Any]]:
    """Bulk import settings (merge semantics per category). Never imports provider keys."""
    invalid = set(request.data.keys()) - _VALID_CATEGORIES
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid categories: {sorted(invalid)}. Valid: {sorted(_VALID_CATEGORIES)}",
        )
    data = dict(request.data)
    if "ai" in data:
        ai_data = {k: v for k, v in data["ai"].items() if k not in _AI_COMPUTED_FIELDS}
        effective = {**get_settings_service().get("ai"), **ai_data}
        try:
            validate_ai_settings(effective)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        data["ai"] = ai_data

    service = get_settings_service()
    try:
        result = await service.bulk_import(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result["ai"] = await _with_ai_credential_status(result["ai"])
    return result
