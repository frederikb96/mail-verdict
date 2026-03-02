"""
Config factory for tests.

Loads real config/config.yaml and applies test-friendly overrides
via deep merge (Engram pattern).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from mail_verdict.config.loader import _deep_merge

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_CONFIG = _PROJECT_ROOT / "config" / "config.yaml"


_TEST_DEFAULTS: dict[str, Any] = {
    "database": {
        "url": "sqlite+aiosqlite://",
        "pool_size": 1,
        "max_overflow": 0,
    },
    "server": {
        "port": 18080,
        "cors_origins": ["http://localhost:5173"],
    },
    "qdrant": {
        "host": "localhost",
        "port": 16334,
    },
    "spam": {
        "enabled": True,
    },
    "sync": {
        "poll_interval_seconds": 1,
        "idle_enabled": False,
    },
    "mcp": {
        "enabled": False,
    },
}


def make_config(**overrides: Any) -> dict[str, Any]:
    """
    Load real config/config.yaml and apply test-friendly overrides.

    Args:
        **overrides: Top-level section overrides (e.g., server={"port": 9999})

    Returns:
        Merged config dict ready for config loader consumption
    """
    with open(_DEFAULT_CONFIG) as f:
        base: dict[str, Any] = yaml.safe_load(f) or {}

    base = copy.deepcopy(base)
    _deep_merge(base, _TEST_DEFAULTS)
    if overrides:
        _deep_merge(base, overrides)

    return base
