"""
Settings API endpoints.

GET /api/settings — all settings by category
GET /api/settings/{category} — single category
PUT /api/settings/{category} — update category (merge)
POST /api/settings/import — bulk import
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mail_verdict.settings import SettingCategory, get_settings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

_VALID_CATEGORIES = {cat.value for cat in SettingCategory}


class SettingsUpdateRequest(BaseModel):
    """Request to update settings for a category."""

    data: dict[str, Any]


class SettingsImportRequest(BaseModel):
    """Request to bulk import settings."""

    data: dict[str, dict[str, Any]]


@router.get("")
async def get_all_settings() -> dict[str, dict[str, Any]]:
    """Get all settings grouped by category."""
    service = get_settings_service()
    return service.get_all()


@router.get("/{category}")
async def get_settings(category: str) -> dict[str, Any]:
    """Get settings for a single category."""
    if category not in _VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid category '{category}'. Valid: {sorted(_VALID_CATEGORIES)}",
        )
    service = get_settings_service()
    return service.get(category)


@router.put("/{category}")
async def update_settings(category: str, request: SettingsUpdateRequest) -> dict[str, Any]:
    """Update settings for a category (merge semantics)."""
    if category not in _VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid category '{category}'. Valid: {sorted(_VALID_CATEGORIES)}",
        )
    service = get_settings_service()
    return await service.update(category, request.data)


@router.post("/import")
async def import_settings(request: SettingsImportRequest) -> dict[str, dict[str, Any]]:
    """Bulk import settings (merge semantics per category)."""
    invalid = set(request.data.keys()) - _VALID_CATEGORIES
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid categories: {sorted(invalid)}. Valid: {sorted(_VALID_CATEGORIES)}",
        )
    service = get_settings_service()
    return await service.bulk_import(request.data)
