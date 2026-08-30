"""
Root conftest: shared fixtures across all test types.
"""

from __future__ import annotations

import pytest

import mail_verdict.config.loader as _loader

# Container fixtures (postgres_container, postimap_container, postgres_url, ...)
# used by tests/pg/ and, later, tests/e2e/. pytest_plugins must live in the
# rootdir conftest -- a non-top-level one is rejected since pytest 8.
pytest_plugins = ["tests.setup.containers"]


@pytest.fixture(autouse=True)
def reset_config() -> None:
    """Clear config singleton between tests."""
    _loader._CONFIG = {}
    _loader._config_instance = None
