"""
Exponential backoff with full jitter for queued retries.
"""

from __future__ import annotations

import random


def compute_backoff(
    attempts: int, *, base_seconds: float, cap_seconds: float,
    rng: random.Random | None = None,
) -> float:
    """
    Compute a full-jitter exponential backoff delay.

    Full jitter -- a uniform draw between 0 and the exponential ceiling,
    rather than a fixed delay -- so that a batch of items failing at the
    same instant does not also retry at the same instant. A fixed delay
    would turn a transient provider blip into a self-inflicted thundering
    herd exactly `base * 2^attempts` seconds later.

    Args:
        attempts: Number of attempts made so far (0 or more)
        base_seconds: Delay after the first attempt
        cap_seconds: Maximum delay regardless of how many attempts
        rng: Source of randomness, injectable for deterministic tests

    Returns:
        Delay in seconds, in [0, cap_seconds]
    """
    ceiling = min(cap_seconds, base_seconds * (2 ** max(attempts, 0)))
    source = rng if rng is not None else random
    return source.uniform(0, ceiling)
