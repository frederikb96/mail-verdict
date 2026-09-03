"""
Settings service: cached DB settings merged with defaults.

Provides typed access to application settings via get_settings_service().
"""

from __future__ import annotations

import logging
from typing import Any

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.settings.defaults import SETTING_DEFAULTS, SettingCategory
from mail_verdict.settings.repository import SettingsRepository

logger = logging.getLogger(__name__)


class SettingsService:
    """
    Cached settings with DB persistence and defaults merge.

    On load(), reads all settings from DB and merges with defaults.
    On update(), writes to DB and refreshes the cache.
    """

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize the settings service.

        Args:
            db: Database connection
        """
        self._repo = SettingsRepository(db)
        self._cache: dict[str, dict[str, Any]] = {}

    async def load(self) -> None:
        """Load all settings from DB, merge with defaults, and cache."""
        db_settings = await self._repo.get_all()
        self._cache = {}
        for cat in SettingCategory:
            defaults = SETTING_DEFAULTS.get(cat, {})
            db_data = db_settings.get(cat, {})
            self._cache[cat] = {**defaults, **db_data}
        logger.info("Settings loaded", extra={"categories": list(self._cache.keys())})

    def get(self, category: str) -> dict[str, Any]:
        """
        Get settings for a category (from cache).

        Falls back to defaults if cache is empty.

        Args:
            category: Setting category name

        Returns:
            Merged settings dict
        """
        if category in self._cache:
            return dict(self._cache[category])
        defaults = SETTING_DEFAULTS.get(category, {})
        return dict(defaults)

    def has_category(self, category: str) -> bool:
        """
        Check if a category exists in the cache.

        Args:
            category: Setting category name
        """
        return category in self._cache

    def get_all(self) -> dict[str, dict[str, Any]]:
        """
        Get all settings (from cache).

        Returns:
            Dict mapping category to merged settings
        """
        result: dict[str, dict[str, Any]] = {}
        for cat in SettingCategory:
            result[cat] = self.get(cat)
        return result

    @staticmethod
    def _validate_types(category: str, data: dict[str, Any]) -> dict[str, Any]:
        """
        Validate setting values match expected types from defaults.

        A whole-number float setting is accepted as an int and coerced --
        JSON (and the JS client sending it) has one number type, so there
        is no way to write a literal "1.0" for a value that happens to be
        1, and rejecting it makes every unmodified default field in a
        settings form unsendable.

        Args:
            category: Setting category name
            data: Settings data to validate

        Returns:
            data, with any int-for-float value coerced to float

        Raises:
            ValueError: If a value has the wrong type
        """
        defaults = SETTING_DEFAULTS.get(category, {})
        coerced = dict(data)
        for key, value in data.items():
            if key not in defaults:
                continue
            expected = type(defaults[key])
            if expected is float and isinstance(value, int) and not isinstance(value, bool):
                coerced[key] = float(value)
                continue
            if not isinstance(value, expected):
                raise ValueError(
                    f"Setting '{category}.{key}' expects "
                    f"{expected.__name__}, got {type(value).__name__}"
                )
        return coerced

    async def update(self, category: str, data: dict[str, Any]) -> dict[str, Any]:
        """
        Update settings for a category (merge semantics).

        Writes to DB and refreshes cache. Validates types first.

        Args:
            category: Setting category name
            data: Partial settings to merge

        Returns:
            Updated merged settings
        """
        data = self._validate_types(category, data)
        await self._repo.upsert_category(category, data)
        defaults = SETTING_DEFAULTS.get(category, {})
        db_data = await self._repo.get_category(category)
        self._cache[category] = {**defaults, **(db_data or {})}
        return dict(self._cache[category])

    async def bulk_import(self, settings: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """
        Bulk import settings (merge semantics per category).

        Type-validates each category the same way update() does -- without
        this, an import accepts a value update() would reject (a string
        where a number belongs, say) and the mistake surfaces much later,
        inside whatever worker first reads it.

        Args:
            settings: Dict mapping category to partial settings data

        Returns:
            All updated settings

        Raises:
            ValueError: If any category's data has a wrongly-typed value
        """
        importable = {
            category: data
            for category, data in settings.items()
            if category in {cat.value for cat in SettingCategory}
        }
        importable = {
            category: self._validate_types(category, data)
            for category, data in importable.items()
        }
        for category, data in importable.items():
            await self._repo.upsert_category(category, data)
        await self.load()
        return self.get_all()


_settings_service: SettingsService | None = None


def get_settings_service() -> SettingsService:
    """
    Get the global settings service.

    Raises:
        RuntimeError: If settings service not initialized
    """
    if _settings_service is None:
        raise RuntimeError("SettingsService not initialized")
    return _settings_service


async def init_settings_service(db: DatabaseConnection) -> SettingsService:
    """
    Initialize and load the global settings service.

    Args:
        db: Database connection

    Returns:
        Initialized SettingsService
    """
    global _settings_service
    _settings_service = SettingsService(db)
    await _settings_service.load()
    return _settings_service


def reset_settings_service() -> None:
    """Reset the global settings service. Useful for testing."""
    global _settings_service
    _settings_service = None
