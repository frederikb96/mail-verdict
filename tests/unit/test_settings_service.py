"""Tests for SettingsService: defaults merge, cache, get/update."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.settings.defaults import SETTING_DEFAULTS, SettingCategory
from mail_verdict.settings.service import SettingsService


def _make_service(
    db_settings: dict[str, dict[str, Any]] | None = None,
) -> SettingsService:
    """Create a SettingsService with a mocked DB connection and repository."""
    db = MagicMock()
    service = SettingsService(db)
    service._repo = MagicMock()
    service._repo.get_all = AsyncMock(return_value=db_settings or {})
    service._repo.get_category = AsyncMock(return_value=None)
    service._repo.upsert_category = AsyncMock()
    return service


class TestDefaults:
    """Tests for default values."""

    def test_all_categories_have_defaults(self) -> None:
        """Every SettingCategory has an entry in SETTING_DEFAULTS."""
        for cat in SettingCategory:
            assert cat in SETTING_DEFAULTS or cat.value in SETTING_DEFAULTS

    def test_ai_defaults(self) -> None:
        """AI defaults include model and embedding config."""
        ai = SETTING_DEFAULTS[SettingCategory.AI]
        assert "model" in ai
        assert "embedding_model" in ai
        assert "embedding_dimensions" in ai

    def test_spam_defaults(self) -> None:
        """Spam defaults include enabled and excerpt_length."""
        spam = SETTING_DEFAULTS[SettingCategory.SPAM]
        assert "enabled" in spam
        assert "excerpt_length" in spam

    def test_retry_defaults(self) -> None:
        """Retry defaults include max_retries and backoff params."""
        retry = SETTING_DEFAULTS[SettingCategory.RETRY]
        assert "max_retries" in retry
        assert "base_delay_seconds" in retry
        assert "exponential_base" in retry


class TestSettingsServiceLoad:
    """Tests for loading and caching."""

    @pytest.mark.asyncio
    async def test_load_empty_db_returns_defaults(self) -> None:
        """Empty DB means all settings come from defaults."""
        service = _make_service()
        await service.load()
        ai = service.get("ai")
        assert ai["model"] == SETTING_DEFAULTS[SettingCategory.AI]["model"]

    @pytest.mark.asyncio
    async def test_load_db_overrides_defaults(self) -> None:
        """DB values override defaults."""
        service = _make_service(db_settings={"ai": {"model": "custom-model"}})
        await service.load()
        ai = service.get("ai")
        assert ai["model"] == "custom-model"
        assert ai["embedding_model"] == SETTING_DEFAULTS[SettingCategory.AI]["embedding_model"]

    @pytest.mark.asyncio
    async def test_get_unknown_category_returns_empty(self) -> None:
        """Unknown category returns empty dict (no defaults)."""
        service = _make_service()
        await service.load()
        result = service.get("nonexistent")
        assert result == {}


class TestSettingsServiceGet:
    """Tests for get and get_all."""

    @pytest.mark.asyncio
    async def test_get_returns_copy(self) -> None:
        """get() returns a copy, not a reference to the cache."""
        service = _make_service()
        await service.load()
        settings = service.get("ai")
        settings["model"] = "tampered"
        assert service.get("ai")["model"] != "tampered"

    @pytest.mark.asyncio
    async def test_get_all_returns_all_categories(self) -> None:
        """get_all() returns a dict for every SettingCategory."""
        service = _make_service()
        await service.load()
        all_settings = service.get_all()
        for cat in SettingCategory:
            assert cat in all_settings or cat.value in all_settings

    @pytest.mark.asyncio
    async def test_has_category(self) -> None:
        """has_category() reflects cache state."""
        service = _make_service()
        assert service.has_category("ai") is False
        await service.load()
        assert service.has_category("ai") is True
        assert service.has_category("nonexistent") is False


class TestSettingsServiceUpdate:
    """Tests for update and bulk_import."""

    @pytest.mark.asyncio
    async def test_update_calls_repo(self) -> None:
        """update() writes to repo and refreshes cache."""
        service = _make_service()
        await service.load()
        service._repo.get_category = AsyncMock(
            return_value={"model": "new-model"}
        )
        result = await service.update("ai", {"model": "new-model"})
        service._repo.upsert_category.assert_awaited_once_with("ai", {"model": "new-model"})
        assert result["model"] == "new-model"

    @pytest.mark.asyncio
    async def test_bulk_import_updates_multiple(self) -> None:
        """bulk_import() updates multiple categories."""
        service = _make_service()
        await service.load()
        await service.bulk_import({
            "ai": {"model": "bulk-model"},
            "spam": {"enabled": False},
        })
        assert service._repo.upsert_category.await_count == 2
