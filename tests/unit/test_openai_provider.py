"""
get_openai_client: caches by key, and every client it hands out carries a
bounded request timeout with the SDK's own retries turned off -- see the
module's own comment for why those two travel together.
"""

from __future__ import annotations

from mail_verdict.core.openai_provider import (
    REQUEST_TIMEOUT_SECONDS,
    get_openai_client,
    reset_openai_provider,
)


class TestClientConstruction:
    def setup_method(self) -> None:
        reset_openai_provider()

    def teardown_method(self) -> None:
        reset_openai_provider()

    def test_no_key_returns_none(self) -> None:
        assert get_openai_client(None) is None
        assert get_openai_client("") is None

    def test_a_key_produces_a_bounded_timeout_and_no_sdk_retries(self) -> None:
        """A hung request must fail within a bound well inside the
        shortest lease any caller holds, and the SDK's own retry loop must
        not silently multiply that wait -- see the module's own comment
        for which callers share this client and why."""
        client = get_openai_client("sk-test")
        assert client is not None
        assert client.timeout == REQUEST_TIMEOUT_SECONDS
        assert client.max_retries == 0

    def test_the_same_key_reuses_the_cached_client(self) -> None:
        first = get_openai_client("sk-test")
        second = get_openai_client("sk-test")
        assert first is second

    def test_a_changed_key_rebuilds_the_client(self) -> None:
        first = get_openai_client("sk-old")
        second = get_openai_client("sk-new")
        assert first is not second
        assert second.timeout == REQUEST_TIMEOUT_SECONDS
