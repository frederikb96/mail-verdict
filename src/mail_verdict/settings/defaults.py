"""
Default values for DB-stored settings.

Single source of truth for all application settings defaults.
Categories: ai, spam, retry, rules.
"""

from __future__ import annotations

import enum
from typing import Any


class SettingCategory(str, enum.Enum):
    """Valid setting categories."""

    AI = "ai"
    SPAM = "spam"
    RETRY = "retry"
    RULES = "rules"


SETTING_DEFAULTS: dict[str, dict[str, Any]] = {
    SettingCategory.AI: {
        # "anthropic" calls the real model and needs ANTHROPIC_API_KEY.
        # "fake" classifies on keywords alone, for local use without a key.
        "provider": "anthropic",
        "model": "claude-haiku-4-5",
        "enrichment_model": "claude-haiku-4-5",
        "max_tokens": 1024,
    },
    SettingCategory.SPAM: {
        "enabled": True,
        "excerpt_length": 300,
        "auto_move_to_junk": True,
        "auto_mark_read": True,
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
