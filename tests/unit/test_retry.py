"""Tests for RetryConfig's full-jitter backoff."""

from __future__ import annotations

from mail_verdict.core.retry import RetryConfig


def _config(**overrides: float) -> RetryConfig:
    defaults = {"max_retries": 5, "base_delay": 1.0, "max_delay": 20.0, "exp_base": 2.0}
    return RetryConfig(**{**defaults, **overrides})


class TestFullJitter:
    """Delay is a uniform draw over [0, cap], not a fixed value."""

    def test_delay_never_exceeds_the_cap(self) -> None:
        config = _config()
        for attempt in range(10):
            cap = min(config.max_delay, config.base_delay * (config.exp_base ** attempt))
            for _ in range(20):
                assert 0 <= config.delay_for_attempt(attempt) <= cap

    def test_delay_never_negative(self) -> None:
        config = _config(base_delay=0.001, max_delay=0.01)
        for _ in range(20):
            assert config.delay_for_attempt(0) >= 0

    def test_repeated_calls_are_not_all_identical(self) -> None:
        """A fixed (non-jittered) backoff would return the same value every time."""
        config = _config(base_delay=5.0, max_delay=20.0)
        delays = {config.delay_for_attempt(3) for _ in range(30)}
        assert len(delays) > 1

    def test_cap_is_respected_even_past_max_delay(self) -> None:
        """Once the exponential term exceeds max_delay, the cap stops growing."""
        config = _config(base_delay=1.0, max_delay=5.0, exp_base=2.0)
        for _ in range(30):
            assert config.delay_for_attempt(10) <= 5.0
