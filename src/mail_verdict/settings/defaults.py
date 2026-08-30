"""
Default values for DB-stored settings.

Single source of truth for all application settings defaults.
Categories: ai, spam, retry, pipeline.

"rules" is not one of them: a rule is a `match` stage in the pipeline
now (see pipeline/stages/match.py), and `settings.rules` -- if a pre-
existing deployment still has one -- is read exactly once, by the
migration that builds the first pipeline revision from it (see
alembic/versions/0006_pipeline.py), never at runtime after that.
"""

from __future__ import annotations

import enum
from typing import Any


class SettingCategory(str, enum.Enum):
    """Valid setting categories."""

    AI = "ai"
    SPAM = "spam"
    RETRY = "retry"
    PIPELINE = "pipeline"


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
    SettingCategory.PIPELINE: {
        "enabled": True,
        # Worker claim/lease mechanics -- see queue/work_queue.py.
        "lease_seconds": 120,
        "poll_interval_seconds": 2.0,
        # Retry backoff for a stage raising StageTransient or an unmapped
        # exception; full jitter, see queue/backoff.py.
        "max_attempts": 5,
        "base_delay_seconds": 2.0,
        "max_delay_seconds": 60.0,
        # How long a suspended or throttled run waits before becoming
        # claimable again -- distinct from the circuit breaker's own
        # probe interval, which gates whether a call is attempted at all.
        "unavailable_probe_seconds": 60,
        # Reconciliation's secondary guard against a missing or stale
        # per-folder watermark: a message older than this is never
        # treated as live-eligible, however its folder's watermark reads.
        "live_max_age_days": 7,
    },
}
