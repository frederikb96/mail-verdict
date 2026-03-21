"""
Default values for DB-stored settings.

Single source of truth for all application settings defaults.
Categories: ai, spam, sync, retry.
"""

from __future__ import annotations

import enum
from typing import Any


class SettingCategory(str, enum.Enum):
    """Valid setting categories."""

    AI = "ai"
    SPAM = "spam"
    SYNC = "sync"
    RETRY = "retry"
    RULES = "rules"


SETTING_DEFAULTS: dict[str, dict[str, Any]] = {
    SettingCategory.AI: {
        "provider": "openai",
        "model": "gpt-5-mini",
        "embedding_model": "text-embedding-3-large",
        "embedding_dimensions": 3072,
        "api_key": "",
    },
    SettingCategory.SPAM: {
        "enabled": True,
        "excerpt_length": 300,
        "neighbor_count": 3,
        "auto_mark_read": True,
    },
    SettingCategory.SYNC: {
        "enabled": True,
        "poll_interval_seconds": 300,
        "idle_enabled": True,
        "idle_restart_seconds": 1500,
    },
    SettingCategory.RETRY: {
        "max_retries": 3,
        "base_delay_seconds": 1.0,
        "max_delay_seconds": 8.0,
        "exponential_base": 2.0,
    },
    SettingCategory.RULES: {
        "rules": [],
    },
}
