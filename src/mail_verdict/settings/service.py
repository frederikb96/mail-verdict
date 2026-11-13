"""
Settings service: cached DB settings merged with defaults.

Provides typed access to application settings via get_settings_service().
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.settings.defaults import (
    SETTING_DEFAULTS,
    SettingCategory,
    user_keys,
)
from mail_verdict.settings.repository import SettingsRepository

logger = logging.getLogger(__name__)


class AdminSettingError(ValueError):
    """A write named settings only an admin may change (see SETTING_SCOPE)."""

    def __init__(self, category: str, keys: list[str]) -> None:
        super().__init__(f"Only an admin may change {category}.{', '.join(sorted(keys))}")
        self.keys = keys


class SettingsService:
    """
    Cached settings with DB persistence and defaults merge.

    On load(), reads all settings from DB and merges with defaults.
    On update(), writes to DB and refreshes the cache.

    Two views of a category exist. get() is the instance view: the stored
    instance values over the defaults, what code acting for nobody reads.
    get_for_user() is one person's view: the instance's admin keys, with
    each user key taken from that person's own values (or the default),
    which is what anything acting for a user reads. Admin keys live in the
    settings table, user keys in user_settings; a user key stored in the
    settings table is never read by get_for_user().
    """

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize the settings service.

        Args:
            db: Database connection
        """
        self._repo = SettingsRepository(db)
        self._cache: dict[str, dict[str, Any]] = {}
        self._user_cache: dict[uuid.UUID, dict[str, dict[str, Any]]] = {}

    async def load(self) -> None:
        """Load all settings from DB, merge with defaults, and cache."""
        db_settings = await self._repo.get_all()
        self._cache = {}
        for cat in SettingCategory:
            defaults = SETTING_DEFAULTS.get(cat, {})
            db_data = db_settings.get(cat, {})
            self._cache[cat] = {**defaults, **db_data}
        self._user_cache = await self._repo.get_all_user_settings()
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

    def get_for_user(self, category: str, user_id: uuid.UUID | None) -> dict[str, Any]:
        """
        One person's view of a category: instance values for the admin keys,
        the person's own values (else the defaults) for the user keys.

        Args:
            category: Setting category name
            user_id: The user, or None for someone with no values of their
                own (work acting for nobody) -- user keys then read their
                defaults

        Returns:
            Merged settings dict
        """
        defaults = SETTING_DEFAULTS.get(category, {})
        view = self.get(category)
        own = self._user_cache.get(user_id, {}).get(category, {}) if user_id else {}
        for key in user_keys(category):
            view[key] = own.get(key, defaults.get(key))
        return view

    def get_user_keys(self, category: str, user_id: uuid.UUID | None) -> dict[str, Any]:
        """get_for_user(), reduced to the keys a user owns: what a person
        who is not an admin may see of a category."""
        view = self.get_for_user(category, user_id)
        return {key: view[key] for key in user_keys(category)}

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
            default = defaults[key]
            if default is None:
                # A None default marks the setting nullable and carries no
                # type to infer, so the only thing knowable here is that
                # clearing it is allowed. Inferring `NoneType` instead makes
                # None the only value the setting will ever accept.
                continue
            expected = type(default)
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

    async def update_for_user(
        self, category: str, user_id: uuid.UUID, data: dict[str, Any], *, is_admin: bool,
    ) -> dict[str, Any]:
        """
        Write a category on behalf of a person: their user keys go to their
        own values, any other key to the instance's -- which only an admin
        may change. Validates types first, and writes nothing if any key is
        refused or ill-typed.

        Args:
            category: Setting category name
            user_id: The person writing
            data: Partial settings to merge
            is_admin: Whether the person may change instance keys

        Returns:
            The person's view of the category after the write (get_for_user)

        Raises:
            AdminSettingError: A key only an admin may change, from a non-admin
            ValueError: A value has the wrong type
        """
        own_keys = user_keys(category)
        own = {key: value for key, value in data.items() if key in own_keys}
        instance = {key: value for key, value in data.items() if key not in own_keys}
        if instance and not is_admin:
            raise AdminSettingError(category, list(instance))
        own = self._validate_types(category, own)
        instance = self._validate_types(category, instance)
        if instance:
            await self.update(category, instance)
        if own:
            stored = await self._repo.upsert_user_category(user_id, category, own)
            self._user_cache.setdefault(user_id, {})[category] = stored
        return self.get_for_user(category, user_id)

    async def bulk_import(
        self, settings: dict[str, dict[str, Any]], user_id: uuid.UUID,
    ) -> dict[str, dict[str, Any]]:
        """
        Bulk import settings as an admin (merge semantics per category).
        Instance keys go to the instance, user keys to the importing admin's
        own values.

        Type-validates each category the same way update() does -- without
        this, an import accepts a value update() would reject (a string
        where a number belongs, say) and the mistake surfaces much later,
        inside whatever worker first reads it.

        Args:
            settings: Dict mapping category to partial settings data
            user_id: The admin importing

        Returns:
            Every category as the importing admin sees it

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
            own_keys = user_keys(category)
            instance = {k: v for k, v in data.items() if k not in own_keys}
            own = {k: v for k, v in data.items() if k in own_keys}
            if instance:
                await self._repo.upsert_category(category, instance)
            if own:
                await self._repo.upsert_user_category(user_id, category, own)
        await self.load()
        return {cat.value: self.get_for_user(cat.value, user_id) for cat in SettingCategory}


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
