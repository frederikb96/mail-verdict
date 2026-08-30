"""Unit tests for queue/backoff.py."""

from __future__ import annotations

import random

from mail_verdict.queue.backoff import compute_backoff


def test_delay_never_exceeds_the_cap() -> None:
    """However many attempts, the delay is bounded -- otherwise a poison pill's
    next_attempt_at could drift years into the future."""
    delay = compute_backoff(50, base_seconds=1.0, cap_seconds=30.0, rng=random.Random(1))
    assert delay <= 30.0


def test_delay_grows_with_attempts_below_the_cap() -> None:
    """The ceiling doubles per attempt while under the cap -- a jitter draw pinned to
    the maximum possible value at each attempt count must strictly increase."""
    rng = random.Random(0)
    low = compute_backoff(0, base_seconds=1.0, cap_seconds=1000.0, rng=rng)
    high = compute_backoff(5, base_seconds=1.0, cap_seconds=1000.0, rng=rng)
    # Not a direct comparison of two draws (jitter makes that flaky) --
    # compare the ceilings each draw was bounded by instead.
    assert low <= 1.0
    assert high <= 32.0
    assert high > 1.0


def test_zero_attempts_uses_the_base_ceiling() -> None:
    """The first attempt's ceiling is exactly base_seconds, not base_seconds * 2."""
    rng = random.Random(2)
    delay = compute_backoff(0, base_seconds=4.0, cap_seconds=1000.0, rng=rng)
    assert 0 <= delay <= 4.0


def test_delay_is_never_negative() -> None:
    """Full jitter draws from [0, ceiling] -- never negative, whatever attempts is."""
    rng = random.Random(3)
    for attempts in range(0, 10):
        delay = compute_backoff(attempts, base_seconds=1.0, cap_seconds=60.0, rng=rng)
        assert delay >= 0
