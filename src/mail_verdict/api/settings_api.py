"""
Settings API endpoints.

GET /api/settings — all settings by category
GET /api/settings/{category} — single category
PUT /api/settings/{category} — update category (merge)
POST /api/settings/import — bulk import

The "ai" category carries provider API keys as a write-only extension:
PUT accepts anthropic_api_key / openai_api_key / custom_api_key (plaintext,
encrypted and stored on write), and every read reports only
anthropic_api_key_configured / anthropic_api_key_hint (and the other
providers' equivalents) -- the key itself is never merged into the JSONB
settings blob and never appears in a response.

A write to "semantic" that changes model, provider or base_url freezes the
identity that was actually serving search into active_model/active_provider
/active_base_url first -- see _freeze_active_embedding_identity. The
worker's backfill reconciler advances those to match once coverage under
the new identity completes (embeddings/worker.py's _maybe_cutover).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mail_verdict.api.events import broadcast_event, get_event_ring
from mail_verdict.database.connection import get_db_connection
from mail_verdict.embeddings.provider import (
    resolve_active_embedding_model,
    resolve_active_embedding_provider,
)
from mail_verdict.settings import SettingCategory, get_settings_service
from mail_verdict.settings.ai_validation import validate_ai_settings, validate_semantic_settings
from mail_verdict.settings.credentials import (
    PROVIDER_ENV_VARS,
    EncryptionUnavailableError,
    get_provider_credential_repo,
)
from mail_verdict.settings.orders_validation import validate_orders_settings

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


async def _announce_settings_changed(category: str | None = None) -> None:
    """
    Push settings.changed to every connected viewer.

    The settings table is MailVerdict's own, with nothing upstream to fire
    a notification on a write to it -- a category changed here reaches
    another open tab only if something pushes it by hand. Broadcast rather
    than scoped to one account: a setting is not account-scoped either, so
    there is no single account_id to key the event on (see broadcast_event).

    Args:
        category: The category that changed, when one write touched only
            one -- omitted for import_settings, which may touch several
    """
    event_ring = get_event_ring()
    if event_ring is None:
        return
    data: dict[str, Any] = {"category": category} if category else {}
    await broadcast_event(get_db_connection(), event_ring, "settings.changed", data)


_EMBEDDING_IDENTITY_FIELDS = ("model", "provider", "base_url")


def _freeze_active_embedding_identity(
    current: dict[str, Any], data: dict[str, Any],
) -> dict[str, Any]:
    """
    Before a write changes semantic.model, .provider or .base_url, snapshot
    whichever identity is actually serving search right now into
    active_model/active_provider/active_base_url -- so search keeps
    answering from it until the reconciler observes full coverage under
    the new one and advances them to match (embeddings/worker.py's
    _maybe_cutover). A no-op once already frozen for an in-flight
    migration another change lands on top of, and never overrides a value
    the caller set explicitly.

    Args:
        current: The semantic settings dict as it reads before this write
        data: The incoming partial update

    Returns:
        data, with active_model/active_provider/active_base_url added when
        an identity field is changing and the caller didn't already set them
    """
    changing = any(
        field in data and str(data[field] or "") != str(current.get(field) or "")
        for field in _EMBEDDING_IDENTITY_FIELDS
    )
    if not changing or "active_model" in data:
        return data
    active_provider, active_base_url = resolve_active_embedding_provider(current)
    return {
        **data,
        "active_model": resolve_active_embedding_model(current),
        "active_provider": active_provider,
        "active_base_url": active_base_url,
    }


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

    if category == "semantic":
        current = service.get("semantic")
        data = _freeze_active_embedding_identity(current, data)
        effective = {**current, **data}
        try:
            validate_semantic_settings(effective)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if category == "orders":
        effective = {**service.get("orders"), **data}
        try:
            validate_orders_settings(effective)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        result = await service.update(category, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _announce_settings_changed(category)
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

    if "semantic" in data:
        current = get_settings_service().get("semantic")
        semantic_data = _freeze_active_embedding_identity(current, data["semantic"])
        effective = {**current, **semantic_data}
        try:
            validate_semantic_settings(effective)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        data["semantic"] = semantic_data

    if "orders" in data:
        effective = {**get_settings_service().get("orders"), **data["orders"]}
        try:
            validate_orders_settings(effective)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    service = get_settings_service()
    try:
        result = await service.bulk_import(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _announce_settings_changed()
    result["ai"] = await _with_ai_credential_status(result["ai"])
    return result
