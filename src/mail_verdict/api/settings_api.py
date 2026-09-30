"""
Settings API endpoints.

GET /api/settings — all settings by category
GET /api/settings/{category} — single category
PUT /api/settings/{category} — update category (merge)
POST /api/settings/import — bulk import

A provider API key is a write-only extension of the JSON any category
accepts: PUT anthropic_api_key / openai_api_key / custom_api_key
(plaintext, encrypted and stored on write, shared across whichever
category names that provider -- see settings/credentials.py), and every
read reports only anthropic_api_key_configured / anthropic_api_key_hint
(and the other providers' equivalents). This is matched by field shape
(_CREDENTIAL_FIELD_PATTERN below) rather than by which category the
request names, so a key-shaped field is stripped -- never merged into a
category's JSONB blob, never returned -- whichever category it is sent
to or read from, not only "ai".

A write to "semantic" that changes model, provider or base_url freezes the
identity that was actually serving search into active_model/active_provider
/active_base_url first -- see _freeze_active_embedding_identity. The
worker's backfill reconciler advances those to match once coverage under
the new identity completes (embeddings/worker.py's _maybe_cutover).
"""

from __future__ import annotations

import logging
import re
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
# A raw provider key (e.g. "openai_api_key") or its computed status (the
# "_configured"/"_hint" suffix a GET reports instead -- see
# _ai_credential_status below), matched by NAME SHAPE rather than by an
# enumerated per-category list. Stripped from every category's write and
# read alike, including a field for a provider not (yet) in
# PROVIDER_ENV_VARS -- there is no list of categories to keep in sync with
# reality here; a key-shaped field is never persisted or returned,
# whichever category it arrives on and whichever provider it names.
_CREDENTIAL_FIELD_PATTERN = re.compile(r"^\w+_api_key(_configured|_hint)?$")


def _strip_credential_shaped_fields(data: dict[str, Any]) -> dict[str, Any]:
    """
    Drop every field shaped like a provider key or its computed status
    from a settings dict, whichever category it belongs to.

    Applied on every write (so nothing credential-shaped is ever merged
    into a category's JSONB blob) and on every read (so a value already
    sitting in a category's stored blob from before this stripping
    existed is masked too, not only a freshly written one).

    Args:
        data: A category's settings dict, incoming or outgoing

    Returns:
        data, with every key matching _CREDENTIAL_FIELD_PATTERN removed
    """
    return {k: v for k, v in data.items() if not _CREDENTIAL_FIELD_PATTERN.match(k)}


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
    Extract and store any provider_api_key fields from a PUT body, for
    whichever category it targets -- the credential store is keyed by
    provider name, not by settings category (see settings/credentials.py:
    "custom" is one key shared by every category whose own provider field
    is set to "custom").

    An empty string clears the stored key; anything else (over)writes it.
    A key-shaped field for a provider this server doesn't know how to
    store (not in PROVIDER_ENV_VARS) is dropped rather than stored or
    raised on -- there is nowhere safe to put it, and the alternative is
    persisting it in the clear.

    Returns the request data with every credential-shaped field removed,
    so none of them are ever merged into the category's JSONB blob.

    Args:
        data: Raw PUT body for any settings category

    Returns:
        data with anthropic_api_key / openai_api_key / custom_api_key and
        the read-only computed status fields popped out

    Raises:
        HTTPException: 400 if a key is set with no ENCRYPTION_KEY configured
    """
    cred_repo = get_provider_credential_repo()
    for provider in PROVIDER_ENV_VARS:
        field_name = f"{provider}_api_key"
        if field_name not in data:
            continue
        value = data[field_name]
        try:
            if value:
                await cred_repo.set_key(provider, str(value))
            else:
                await cred_repo.clear_key(provider)
        except EncryptionUnavailableError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _strip_credential_shaped_fields(data)


@router.get("")
async def get_all_settings() -> dict[str, dict[str, Any]]:
    """Get all settings grouped by category."""
    service = get_settings_service()
    all_settings = {
        category: _strip_credential_shaped_fields(data)
        for category, data in service.get_all().items()
    }
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
    data = _strip_credential_shaped_fields(service.get(category))
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
    # Applied whichever category this is: a provider key is shared across
    # categories by provider name, not scoped to "ai" (settings/
    # credentials.py), and the request body is an arbitrary dict for every
    # category alike -- nothing about the route restricts a key-shaped
    # field to arriving only where one is expected.
    data = await _apply_credential_writes(request.data)

    if category == "ai":
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
    # Defensive as well as prospective: masks a credential-shaped field
    # already sitting in this category's stored blob from before writes
    # were stripped everywhere, not only one this request just wrote.
    result = _strip_credential_shaped_fields(result)
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
    # A key-shaped field is stripped from every category's import payload,
    # not only "ai" -- import never stores a provider key at all (unlike a
    # PUT, which routes it to the credential store), so this is a plain
    # drop rather than a call to _apply_credential_writes.
    data = {
        category: _strip_credential_shaped_fields(cat_data)
        for category, cat_data in request.data.items()
    }
    if "ai" in data:
        effective = {**get_settings_service().get("ai"), **data["ai"]}
        try:
            validate_ai_settings(effective)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
    result = {
        category: _strip_credential_shaped_fields(cat_data)
        for category, cat_data in result.items()
    }
    result["ai"] = await _with_ai_credential_status(result["ai"])
    return result
