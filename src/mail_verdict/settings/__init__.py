"""MailVerdict settings: DB-stored application configuration."""

from mail_verdict.settings.defaults import SETTING_DEFAULTS, SettingCategory
from mail_verdict.settings.service import SettingsService, get_settings_service

__all__ = [
    "SETTING_DEFAULTS",
    "SettingCategory",
    "SettingsService",
    "get_settings_service",
]
