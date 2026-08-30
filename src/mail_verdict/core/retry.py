"""
RetryConfig: single source of truth for retry behavior.

Replaces 6 copy-paste retry extraction patterns across the codebase.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from mail_verdict.settings.defaults import SETTING_DEFAULTS


@dataclass(frozen=True)
class RetryConfig:
    """Immutable retry configuration."""

    max_retries: int
    base_delay: float
    max_delay: float
    exp_base: float

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> RetryConfig:
        """
        Create RetryConfig from a settings dict, falling back to defaults.

        Args:
            settings: Retry settings dict from SettingsService
        """
        defaults = SETTING_DEFAULTS.get("retry", {})
        return cls(
            max_retries=int(
                settings.get("max_retries", defaults.get("max_retries", 3))
            ),
            base_delay=float(
                settings.get("base_delay_seconds", defaults.get("base_delay_seconds", 1.0))
            ),
            max_delay=float(
                settings.get("max_delay_seconds", defaults.get("max_delay_seconds", 8.0))
            ),
            exp_base=float(
                settings.get("exponential_base", defaults.get("exponential_base", 2.0))
            ),
        )

    def delay_for_attempt(self, attempt: int) -> float:
        """
        Calculate a full-jitter exponential backoff delay for a retry attempt.

        Full jitter (a uniform draw over [0, cap], not a fixed backoff plus
        noise) is what keeps many workers retrying the same provider at once
        from re-synchronizing on the next attempt.

        Args:
            attempt: Zero-indexed attempt number
        """
        cap = min(self.max_delay, self.base_delay * (self.exp_base ** attempt))
        return random.uniform(0, cap)
