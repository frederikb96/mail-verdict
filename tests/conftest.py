"""
Root conftest: shared fixtures across all test types.
"""

from __future__ import annotations

import pytest

import mail_verdict.config.loader as _loader


@pytest.fixture(autouse=True)
def reset_config() -> None:
    """Clear config singleton between tests."""
    _loader._CONFIG = {}
    _loader._config_instance = None
