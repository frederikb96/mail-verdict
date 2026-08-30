"""
Default values for DB-stored settings.

Single source of truth for all application settings defaults.
Categories: ai, spam, retry, rules, semantic.
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
    SEMANTIC = "semantic"


SETTING_DEFAULTS: dict[str, dict[str, Any]] = {
    SettingCategory.AI: {
        # "openai" and "anthropic" both need their provider's API key
        # configured (settings/credentials.py, or the matching env var).
        # "fake" classifies on keywords alone, for local use without a key.
        "provider": "openai",
        "model": "gpt-5.4-nano",
        # "none" matches gpt-5.4-nano's own server-side default. Raising
        # this is a per-model gamble, not a guaranteed quality lever: a
        # lightweight classification task may not spend any reasoning
        # tokens at higher effort either, so measure before assuming a
        # higher setting changes anything for a given model.
        "reasoning_effort": "none",
        "enrichment_model": "gpt-5.4-nano",
        "max_tokens": 1024,
    },
    SettingCategory.SPAM: {
        "enabled": True,
        "excerpt_length": 300,
        "auto_move_to_junk": True,
        "auto_mark_read": True,
    },
    SettingCategory.RETRY: {
        "max_retries": 5,
        "base_delay_seconds": 1.0,
        "max_delay_seconds": 20.0,
        "exponential_base": 2.0,
    },
    SettingCategory.RULES: {
        "rules": [],
    },
    SettingCategory.SEMANTIC: {
        # "openai" is the only real provider -- Anthropic has no embedding
        # model of its own to select here, unlike ai.provider. "fake"
        # produces deterministic hash-derived vectors for local use
        # without a key.
        "provider": "openai",
        "model": "text-embedding-3-small",
        # Every model is asked to truncate to EMBEDDING_DIMENSIONS
        # (database/models.py) via its own dimensions parameter, so this
        # is never a setting -- changing the column width is a migration.
        "content_chars": 2000,
        "batch_size": 64,
        "concurrency": 2,
    },
}
