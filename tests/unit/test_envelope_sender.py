"""envelope_sender: the bare address out of a Return-Path header value."""

from __future__ import annotations

import pytest

from mail_verdict.core.envelope_sender import envelope_sender


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("<bounce@bulk.example.net>", "bounce@bulk.example.net"),
        ("bounce@bulk.example.net", "bounce@bulk.example.net"),
        ("<>", None),
        ("", None),
        (None, None),
    ],
)
def test_envelope_sender(header: str | None, expected: str | None) -> None:
    assert envelope_sender(header) == expected
