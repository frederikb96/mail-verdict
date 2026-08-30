"""
`setup_logging` owns the root handler it installs, and only that one.

Clearing the root logger's handlers wholesale takes any handler a host
attached with them -- pytest's own log capture most visibly, which then
stays gone for every later test in the session, so an assertion on a log
line passes or fails for a reason unrelated to what it asserts.
"""

from __future__ import annotations

import logging

import pytest

from mail_verdict.core.logging import setup_logging

pytestmark = pytest.mark.unit


class TestSetupLogging:
    def test_leaves_a_handler_it_did_not_install_attached(self) -> None:
        root = logging.getLogger()
        foreign = logging.NullHandler()
        root.addHandler(foreign)
        try:
            setup_logging("INFO")
            assert foreign in root.handlers
        finally:
            root.removeHandler(foreign)

    def test_a_second_call_does_not_stack_a_second_json_handler(self) -> None:
        root = logging.getLogger()
        setup_logging("INFO")
        after_first = len(root.handlers)
        setup_logging("INFO")
        assert len(root.handlers) == after_first

    def test_capture_still_works_after_it_runs(self, caplog: pytest.LogCaptureFixture) -> None:
        """The concrete symptom: caplog is a root handler, so a wholesale
        clear silently disables it for the rest of the session."""
        setup_logging("INFO")
        with caplog.at_level(logging.INFO, logger="mail_verdict.test_probe"):
            logging.getLogger("mail_verdict.test_probe").info("probe line")
        assert "probe line" in caplog.text
