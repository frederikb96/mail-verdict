"""Tests for the shared strict-schema retry/dispatch helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.core.errors import ProviderUnavailableError
from mail_verdict.core.retry import RetryConfig
from mail_verdict.core.structured_llm import resolve_client, retry_structured_call


def _fast_retry(max_retries: int = 2) -> RetryConfig:
    return RetryConfig(
        max_retries=max_retries, base_delay=0.001, max_delay=0.005, exp_base=2.0,
    )


class _Transient(Exception):
    """Stand-in for a rate limit / connection error."""


class _Permanent(Exception):
    """Stand-in for a bad request / auth failure -- never worth retrying."""


class TestRetryStructuredCall:
    """Tests for retry_structured_call's failure classification."""

    @pytest.mark.asyncio
    async def test_succeeds_first_try(self) -> None:
        call_once = AsyncMock(return_value='{"a": 1}')
        result = await retry_structured_call(call_once, _fast_retry(), transient_errors=())
        assert result == {"a": 1}
        call_once.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retries_malformed_json_then_succeeds(self) -> None:
        call_once = AsyncMock(side_effect=["not json", '{"a": 1}'])
        result = await retry_structured_call(call_once, _fast_retry(), transient_errors=())
        assert result == {"a": 1}
        assert call_once.await_count == 2

    @pytest.mark.asyncio
    async def test_retries_transient_error_then_succeeds(self) -> None:
        call_once = AsyncMock(side_effect=[_Transient("rate limited"), '{"a": 1}'])
        result = await retry_structured_call(
            call_once, _fast_retry(), transient_errors=(_Transient,),
        )
        assert result == {"a": 1}
        assert call_once.await_count == 2

    @pytest.mark.asyncio
    async def test_non_transient_error_propagates_without_retrying(self) -> None:
        """A bug in the request (bad model, auth failure) is not swallowed into a retry loop."""
        call_once = AsyncMock(side_effect=_Permanent("invalid model"))
        with pytest.raises(_Permanent):
            await retry_structured_call(call_once, _fast_retry(), transient_errors=(_Transient,))
        call_once.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_validate_failure_is_retried(self) -> None:
        call_once = AsyncMock(side_effect=['{"a": 1}', '{"a": 2}'])

        def validate(data: dict[str, object]) -> None:
            if data["a"] != 2:
                raise ValueError("not yet")

        result = await retry_structured_call(
            call_once, _fast_retry(), transient_errors=(), validate=validate,
        )
        assert result == {"a": 2}

    @pytest.mark.asyncio
    async def test_exhausted_retries_raises_runtime_error(self) -> None:
        call_once = AsyncMock(return_value="not json")
        with pytest.raises(RuntimeError, match="failed after"):
            await retry_structured_call(call_once, _fast_retry(max_retries=1), transient_errors=())
        assert call_once.await_count == 2


class TestResolveClient:
    """Tests for provider client resolution."""

    @pytest.mark.asyncio
    async def test_no_key_raises_provider_unavailable(self) -> None:
        cred_repo = MagicMock()
        cred_repo.resolve_key = AsyncMock(return_value=None)
        with pytest.raises(ProviderUnavailableError):
            await resolve_client("anthropic", cred_repo)

    @pytest.mark.asyncio
    async def test_unsupported_provider_raises_value_error(self) -> None:
        cred_repo = MagicMock()
        cred_repo.resolve_key = AsyncMock(return_value="some-key")
        with pytest.raises(ValueError, match="Unsupported provider"):
            await resolve_client("not-a-real-provider", cred_repo)
