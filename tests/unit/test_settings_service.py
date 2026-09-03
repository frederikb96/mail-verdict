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
        """AI defaults include the provider, model and effort settings."""
        ai = SETTING_DEFAULTS[SettingCategory.AI]
        assert "provider" in ai
        assert "model" in ai
        assert "reasoning_effort" in ai
        assert "max_tokens" in ai

    def test_semantic_enabled_has_a_default(self) -> None:
        """semantic.enabled is read at runtime (embeddings/worker.py) and must
        have a default here -- there is nowhere else for one to live."""
        semantic = SETTING_DEFAULTS[SettingCategory.SEMANTIC]
        assert "enabled" in semantic

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
        assert ai["max_tokens"] == SETTING_DEFAULTS[SettingCategory.AI]["max_tokens"]

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
            "retry": {"max_retries": 3},
        })
        assert service._repo.upsert_category.await_count == 2

    @pytest.mark.asyncio
    async def test_bulk_import_rejects_a_wrongly_typed_value(self) -> None:
        """
        bulk_import() must reject a value update() would reject too --
        otherwise a type mistake is silently stored and surfaces later
        wherever a worker first reads it, instead of at write time.
        """
        service = _make_service()
        await service.load()

        with pytest.raises(ValueError, match="retry.max_retries"):
            await service.bulk_import({"retry": {"max_retries": "banana"}})

        service._repo.upsert_category.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_coerces_an_int_to_a_float_setting(self) -> None:
        """
        A whole-number float setting (base_delay_seconds=1.0) round-trips
        through JSON as a bare int -- update() must accept that rather than
        rejecting it as a type mismatch, since a JS client can never send
        a literal "1.0" for the number 1.
        """
        service = _make_service()
        await service.load()
        service._repo.get_category = AsyncMock(return_value={"base_delay_seconds": 1.0})

        result = await service.update("retry", {"base_delay_seconds": 1})

        service._repo.upsert_category.assert_awaited_once_with(
            "retry", {"base_delay_seconds": 1.0},
        )
        assert result["base_delay_seconds"] == 1.0

    @pytest.mark.asyncio
    async def test_bulk_import_coerces_an_int_to_a_float_setting(self) -> None:
        """bulk_import() gets the same int-for-float leniency as update()."""
        service = _make_service()
        await service.load()

        await service.bulk_import({"retry": {"base_delay_seconds": 1}})

        service._repo.upsert_category.assert_awaited_once_with(
            "retry", {"base_delay_seconds": 1.0},
        )

    @pytest.mark.asyncio
    async def test_update_still_rejects_a_bool_for_a_float_setting(self) -> None:
        """Coercion is int-to-float only -- a bool must not slip through as 0.0/1.0."""
        service = _make_service()
        await service.load()

        with pytest.raises(ValueError, match="retry.base_delay_seconds"):
            await service.update("retry", {"base_delay_seconds": True})

    @pytest.mark.asyncio
    async def test_bulk_import_writes_nothing_if_any_category_is_invalid(self) -> None:
        """
        A multi-category import is all-or-nothing: one bad value must not
        leave an earlier, valid category in the import half-applied.
        """
        service = _make_service()
        await service.load()

        with pytest.raises(ValueError):
            await service.bulk_import({
                "ai": {"model": "bulk-model"},
                "retry": {"max_retries": "banana"},
            })

        service._repo.upsert_category.assert_not_awaited()
