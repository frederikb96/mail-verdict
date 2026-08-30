"""
Root conftest: shared fixtures across all test types.
"""

from __future__ import annotations

import os

import pytest

import mail_verdict.config.loader as _loader

# Container fixtures (postgres_container, postimap_container, postgres_url, ...)
# used by tests/pg/ and tests/e2e/. pytest_plugins must live in the
# rootdir conftest -- a non-top-level one is rejected since pytest 8.
pytest_plugins = ["tests.setup.containers"]

_LAYER_DIRS = {"pg": pytest.mark.pg, "e2e": pytest.mark.e2e}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-mark every test by its top-level layer directory.

    A `pytestmark` assigned inside a directory's own conftest.py only
    applies within the module that sets it, not to sibling test modules in
    that directory -- so `-m pg` / `-m e2e` need every test tagged here
    instead, from the one place that sees the whole collected tree.
    """
    for item in items:
        for layer, marker in _LAYER_DIRS.items():
            if f"{os.sep}tests{os.sep}{layer}{os.sep}" in str(item.fspath):
                item.add_marker(marker)
                break


@pytest.fixture(autouse=True)
def reset_config() -> None:
    """Clear config singleton between tests."""
    _loader._CONFIG = {}
    _loader._config_instance = None
