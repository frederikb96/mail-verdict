"""Tests for the shared strict-schema retry/dispatch helper."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.core.errors import ProviderUnavailableError
from mail_verdict.core.openai_provider import reset_openai_provider
from mail_verdict.core.retry import RetryConfig
from mail_verdict.core.structured_llm import (
    call_chat_completions_structured,
    resolve_client,
    retry_after_from_exception,
    retry_structured_call,
)


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

    @pytest.mark.asyncio
    async def test_exhausted_retries_chains_the_last_underlying_error(self) -> None:
        """A caller needing to know *what kind* of failure this was (a
        sustained rate limit, say -- see ModelGateway._map_and_raise)
        must still be able to recover it via __cause__ rather than losing
        it to the generic RuntimeError wrapper."""
        call_once = AsyncMock(side_effect=_Transient("rate limited"))
        with pytest.raises(RuntimeError) as exc_info:
            await retry_structured_call(
                call_once, _fast_retry(max_retries=1), transient_errors=(_Transient,),
            )
        assert isinstance(exc_info.value.__cause__, _Transient)


class TestResolveClient:
    """Tests for provider client resolution."""

    def setup_method(self) -> None:
        reset_openai_provider()

    def teardown_method(self) -> None:
        reset_openai_provider()

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

    @pytest.mark.asyncio
    async def test_custom_provider_without_base_url_raises_provider_unavailable(self) -> None:
        """Defence in depth for a caller that bypasses settings write-time
        validation (settings/ai_validation.py) -- never silently falls
        back to real OpenAI's own endpoint with a compatible server's key."""
        cred_repo = MagicMock()
        cred_repo.resolve_key = AsyncMock(return_value="some-key")
        with pytest.raises(ProviderUnavailableError, match="base_url"):
            await resolve_client("custom", cred_repo)
        cred_repo.resolve_key.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_custom_provider_resolves_a_client_pointed_at_base_url(self) -> None:
        cred_repo = MagicMock()
        cred_repo.resolve_key = AsyncMock(return_value="custom-key")
        client = await resolve_client("custom", cred_repo, base_url="https://example.test/v1")
        assert str(client.base_url).rstrip("/") == "https://example.test/v1"
        cred_repo.resolve_key.assert_awaited_once_with("custom")


class TestCallChatCompletionsStructured:
    """The request shape a "custom" (chat-completions-only) provider needs."""

    def _client(self, content: str | None) -> MagicMock:
        client = MagicMock()
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content=content))]
        client.chat.completions.create = AsyncMock(return_value=response)
        return client

    @pytest.mark.asyncio
    async def test_sends_a_json_schema_response_format(self) -> None:
        client = self._client('{"a": 1}')
        result = await call_chat_completions_structured(
            client, "some-model", None, 512, "schema_name", "system", "user",
            {"type": "object"}, _fast_retry(),
        )
        assert result == {"a": 1}
        kwargs = client.chat.completions.create.await_args.kwargs
        assert kwargs["response_format"]["type"] == "json_schema"
        assert kwargs["response_format"]["json_schema"]["name"] == "schema_name"
        assert kwargs["response_format"]["json_schema"]["strict"] is True
        assert kwargs["max_tokens"] == 512

    @pytest.mark.asyncio
    async def test_reasoning_effort_sent_via_extra_body_when_set(self) -> None:
        client = self._client('{"a": 1}')
        await call_chat_completions_structured(
            client, "some-model", "low", 512, "schema_name", "system", "user",
            {"type": "object"}, _fast_retry(),
        )
        kwargs = client.chat.completions.create.await_args.kwargs
        assert kwargs["extra_body"] == {"reasoning_effort": "low"}

    @pytest.mark.asyncio
    async def test_none_effort_is_sent_explicitly_not_omitted(self) -> None:
        """A compatible server's reasoning models reason by default, so
        omitting the field means "reason", not "don't" -- measured
        directly against Infomaniak's Qwen3.5-122B-A10B-FP8: 2,000+
        hidden reasoning tokens and 15-20s per call with the field left
        out, under a second with "none" sent explicitly."""
        client = self._client('{"a": 1}')
        await call_chat_completions_structured(
            client, "some-model", "none", 512, "schema_name", "system", "user",
            {"type": "object"}, _fast_retry(),
        )
        kwargs = client.chat.completions.create.await_args.kwargs
        assert kwargs["extra_body"] == {"reasoning_effort": "none"}

    @pytest.mark.asyncio
    async def test_a_genuinely_unset_effort_omits_extra_body(self) -> None:
        client = self._client('{"a": 1}')
        await call_chat_completions_structured(
            client, "some-model", None, 512, "schema_name", "system", "user",
            {"type": "object"}, _fast_retry(),
        )
        kwargs = client.chat.completions.create.await_args.kwargs
        assert "extra_body" not in kwargs

    @pytest.mark.asyncio
    async def test_null_content_is_treated_as_malformed_and_retried(self) -> None:
        """An exhausted output budget on a reasoning model returns
        content: null with HTTP 200 (see the module docstring) -- this
        must fail json.loads and be retried, never raise a TypeError."""
        client = self._client(None)
        with pytest.raises(RuntimeError, match="failed after"):
            await call_chat_completions_structured(
                client, "some-model", "low", 512, "schema_name", "system", "user",
                {"type": "object"}, _fast_retry(max_retries=1),
            )
        assert client.chat.completions.create.await_count == 2

    @pytest.mark.asyncio
    async def test_timeout_seconds_omitted_by_default(self) -> None:
        """Leaving timeout_seconds unset must not pass timeout at all --
        the client's own configured default (openai_provider.py) has to
        keep governing classify and embeddings' calls unchanged."""
        client = self._client('{"a": 1}')
        await call_chat_completions_structured(
            client, "some-model", None, 512, "schema_name", "system", "user",
            {"type": "object"}, _fast_retry(),
        )
        kwargs = client.chat.completions.create.await_args.kwargs
        assert "timeout" not in kwargs

    @pytest.mark.asyncio
    async def test_timeout_seconds_forwarded_when_given(self) -> None:
        client = self._client('{"a": 1}')
        await call_chat_completions_structured(
            client, "some-model", None, 512, "schema_name", "system", "user",
            {"type": "object"}, _fast_retry(), timeout_seconds=40.0,
        )
        kwargs = client.chat.completions.create.await_args.kwargs
        assert kwargs["timeout"] == 40.0


class TestRetryAfterFromException:
    """retry_after_from_exception: an adaptive 429 backoff, read from the
    server's own Retry-After header rather than a blind guess."""

    def _exc_with_header(self, value: str | None) -> Exception:
        exc = Exception("rate limited")
        response = MagicMock()
        response.headers = {"retry-after": value} if value is not None else {}
        exc.response = response  # type: ignore[attr-defined]
        return exc

    def test_reads_a_plain_integer_seconds_header(self) -> None:
        exc = self._exc_with_header("12")
        assert retry_after_from_exception(exc, default=timedelta(seconds=30)) == timedelta(
            seconds=12
        )

    def test_missing_header_falls_back_to_default(self) -> None:
        exc = self._exc_with_header(None)
        assert retry_after_from_exception(exc, default=timedelta(seconds=30)) == timedelta(
            seconds=30
        )

    def test_unparseable_header_falls_back_to_default(self) -> None:
        """An HTTP-date form (valid per RFC 9110) is not parsed here --
        the safe default covers it rather than raising."""
        exc = self._exc_with_header("Wed, 21 Oct 2026 07:28:00 GMT")
        assert retry_after_from_exception(exc, default=timedelta(seconds=30)) == timedelta(
            seconds=30
        )

    def test_no_response_attribute_falls_back_to_default(self) -> None:
        """A connection error, or a re-raised exception with nothing
        attached -- must not raise trying to read a header that isn't
        there."""
        exc = Exception("connection dropped")
        assert retry_after_from_exception(exc, default=timedelta(seconds=30)) == timedelta(
            seconds=30
        )
