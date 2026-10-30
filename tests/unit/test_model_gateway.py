"""
ModelGateway._map_and_raise: classifying a provider SDK exception into
the stage vocabulary correctly even after retry_structured_call has
already exhausted its own retry budget and wrapped the real error in a
generic RuntimeError (core/structured_llm.py's own docstring on that).

A sustained rate limit must still reach StageThrottled -- refunded,
uncapped -- rather than falling through to the generic StageTransient
branch, which counts against pipeline_runs.attempts and can eventually
dead-letter a message a throttle should have kept retrying forever.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import httpx
import openai
import pytest

from mail_verdict.core.errors import ModelRefused, ProviderUnavailableError
from mail_verdict.pipeline.context import ModelGateway
from mail_verdict.pipeline.contracts import (
    StageMisconfigured,
    StageRefused,
    StageThrottled,
    StageTransient,
    StageUnavailable,
)


class RateLimitError(Exception):
    """Stands in for openai.RateLimitError -- _map_and_raise matches by
    bare class name, so this need not import the real SDK type. Deliberately
    not underscore-prefixed: the name itself is what's under test."""


class AuthenticationError(Exception):
    """Stands in for openai.AuthenticationError, same reasoning."""


def _gateway() -> ModelGateway:
    return ModelGateway(db=None, cred_repo=None, retry_config=None)  # type: ignore[arg-type]


class TestMapAndRaise:
    @pytest.mark.asyncio
    async def test_a_bare_rate_limit_error_becomes_stage_throttled(self) -> None:
        with pytest.raises(StageThrottled):
            await _gateway()._map_and_raise("openai", RateLimitError("429"))

    @pytest.mark.asyncio
    async def test_a_rate_limit_error_wrapped_by_exhausted_retries_is_still_throttled(
        self,
    ) -> None:
        """The actual reproduction: retry_structured_call's own exhaustion
        wraps the last error in RuntimeError -- this must be unwrapped via
        __cause__, not fall through to the generic transient-failure path."""
        wrapped = RuntimeError("LLM structured call failed after 3 attempts: 429")
        wrapped.__cause__ = RateLimitError("429")
        with pytest.raises(StageThrottled):
            await _gateway()._map_and_raise("openai", wrapped)

    @pytest.mark.asyncio
    async def test_a_runtime_error_with_no_cause_falls_through_to_transient(self) -> None:
        """A bare RuntimeError with nothing chained -- e.g. from
        retry_structured_call exhausting on malformed JSON, never a
        provider exception at all -- has no better classification than
        the generic transient path."""
        with pytest.raises(StageTransient):
            await _gateway()._map_and_raise("openai", RuntimeError("malformed JSON"))

    @pytest.mark.asyncio
    async def test_an_authentication_error_becomes_stage_unavailable(self) -> None:
        with pytest.raises(StageUnavailable):
            await _gateway()._map_and_raise("openai", AuthenticationError("401"))

    @pytest.mark.asyncio
    async def test_an_authentication_error_wrapped_by_exhausted_retries_is_still_unavailable(
        self,
    ) -> None:
        wrapped = RuntimeError("LLM structured call failed after 3 attempts: 401")
        wrapped.__cause__ = AuthenticationError("401")
        with pytest.raises(StageUnavailable):
            await _gateway()._map_and_raise("openai", wrapped)

    @pytest.mark.asyncio
    async def test_an_unrelated_error_becomes_stage_transient(self) -> None:
        with pytest.raises(StageTransient):
            await _gateway()._map_and_raise("openai", ConnectionError("dropped"))


class _RecordingCircuit:
    """Stands in for the breaker row: remembers how the gateway left it."""

    def __init__(self) -> None:
        self.unavailable: list[str] = []
        self.backoffs: list[str | None] = []

    async def record_unavailable(self, *, reason: str, probe_interval: object) -> None:
        self.unavailable.append(reason)

    async def record_backoff(self, *, retry_after: object, reason: str | None = None) -> None:
        self.backoffs.append(reason)


def _recording_gateway() -> tuple[ModelGateway, _RecordingCircuit]:
    gateway = _gateway()
    circuit = _RecordingCircuit()
    gateway._circuit = circuit  # type: ignore[assignment]
    return gateway, circuit


def _anthropic_error(
    cls: type[anthropic.APIStatusError], status: int, error_type: str, message: str,
) -> anthropic.APIStatusError:
    """A real SDK exception as the client raises it for this response."""
    body = {"type": "error", "error": {"type": error_type, "message": message}}
    response = httpx.Response(
        status, json=body, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    return cls(message, response=response, body=body)


class TestExhaustedProviders:
    """A provider out of credit or allowance suspends its breaker and
    releases the job untouched, instead of failing it after its retries."""

    @pytest.mark.asyncio
    async def test_the_credit_balance_400_suspends_the_provider(self) -> None:
        gateway, circuit = _recording_gateway()
        exc = _anthropic_error(
            anthropic.BadRequestError, 400, "invalid_request_error",
            "Your credit balance is too low to access the Anthropic API.",
        )
        with pytest.raises(StageUnavailable, match="credit balance exhausted"):
            await gateway._map_and_raise("anthropic", exc)
        assert circuit.unavailable == ["anthropic credit balance exhausted"]

    @pytest.mark.asyncio
    async def test_a_billing_error_body_suspends_the_provider(self) -> None:
        gateway, circuit = _recording_gateway()
        exc = _anthropic_error(
            anthropic.APIStatusError, 402, "billing_error", "Payment required.",
        )
        with pytest.raises(StageUnavailable):
            await gateway._map_and_raise("anthropic", exc)
        assert circuit.unavailable == ["anthropic credit balance exhausted"]

    @pytest.mark.asyncio
    async def test_permission_denied_suspends_the_provider(self) -> None:
        gateway, circuit = _recording_gateway()
        exc = _anthropic_error(
            anthropic.PermissionDeniedError, 403, "permission_error", "Not allowed.",
        )
        with pytest.raises(StageUnavailable, match="rejected the API key"):
            await gateway._map_and_raise("anthropic", exc)
        assert circuit.unavailable == ["anthropic rejected the API key"]

    @pytest.mark.asyncio
    async def test_an_ordinary_bad_request_is_not_a_suspension(self) -> None:
        gateway, circuit = _recording_gateway()
        exc = _anthropic_error(
            anthropic.BadRequestError, 400, "invalid_request_error", "max_tokens is too large",
        )
        with pytest.raises(StageTransient):
            await gateway._map_and_raise("anthropic", exc)
        assert circuit.unavailable == []

    @pytest.mark.asyncio
    async def test_a_spent_allowance_429_suspends_instead_of_throttling(self) -> None:
        gateway, circuit = _recording_gateway()
        body = {
            "error": {"message": "maximum_token_reached", "type": "maximum_token_reached_error"},
        }
        response = httpx.Response(
            429, json=body, request=httpx.Request("POST", "https://example.test/v1/chat"),
        )
        exc = openai.RateLimitError("maximum_token_reached", response=response, body=body)
        with pytest.raises(StageUnavailable, match="token allowance spent"):
            await gateway._map_and_raise("custom", exc)
        assert circuit.unavailable == ["custom token allowance spent"]
        assert circuit.backoffs == []

    @pytest.mark.asyncio
    async def test_a_plain_429_stays_a_throttle(self) -> None:
        gateway, circuit = _recording_gateway()
        body = {"error": {"message": "Too many requests", "type": "rate_limit_error"}}
        response = httpx.Response(
            429, json=body, request=httpx.Request("POST", "https://example.test/v1/chat"),
        )
        exc = openai.RateLimitError("Too many requests", response=response, body=body)
        with pytest.raises(StageThrottled):
            await gateway._map_and_raise("custom", exc)
        assert circuit.unavailable == []

    @pytest.mark.asyncio
    async def test_exhaustion_wrapped_by_exhausted_retries_is_still_a_suspension(self) -> None:
        gateway, _ = _recording_gateway()
        body = {"error": {"message": "maximum_token_reached", "type": "x"}}
        response = httpx.Response(
            429, json=body, request=httpx.Request("POST", "https://example.test/v1/chat"),
        )
        wrapped = RuntimeError("LLM structured call failed after 6 attempts")
        wrapped.__cause__ = openai.RateLimitError("m", response=response, body=body)
        with pytest.raises(StageUnavailable):
            await gateway._map_and_raise("custom", wrapped)


class TestRefusal:
    @pytest.mark.asyncio
    async def test_a_refusal_fails_the_job_at_once_without_touching_the_breaker(self) -> None:
        gateway, circuit = _recording_gateway()
        with pytest.raises(StageRefused, match="refusal \\(cyber\\)"):
            await gateway._map_and_raise("anthropic", ModelRefused("cyber"))
        assert circuit.unavailable == [] and circuit.backoffs == []

    @pytest.mark.asyncio
    async def test_a_refusal_is_a_misconfigured_stage_so_every_queue_fails_it_at_once(
        self,
    ) -> None:
        gateway, _ = _recording_gateway()
        with pytest.raises(StageMisconfigured):
            await gateway._map_and_raise("anthropic", ModelRefused())


class _ProbingCircuit:
    """A suspended breaker whose probe slot is or is not free."""

    def __init__(self, *, probe_free: bool) -> None:
        self._probe_free = probe_free
        self.successes = 0

    async def is_available(self) -> bool:
        return False

    async def status(self) -> object:
        return SimpleNamespace(state=SimpleNamespace(value="suspended"), reason="no key")

    async def try_probe(self, *, probe_interval: object) -> bool:
        return self._probe_free

    async def record_success(self) -> None:
        self.successes += 1

    async def record_unavailable(self, *, reason: str, probe_interval: object) -> None:
        return None


class TestSuspendedProbe:
    """A suspended breaker clears only on a successful call, so the gateway
    lets one caller per probe interval make it."""

    @staticmethod
    def _call(gateway: ModelGateway) -> object:
        return gateway.structured_call(
            provider="anthropic", category="ai", model="m", effort=None, max_tokens=500,
            schema_name="s", system_prompt="s", user_prompt="u", schema={},
        )

    @pytest.mark.asyncio
    async def test_the_caller_that_wins_the_probe_slot_makes_the_call(self) -> None:
        gateway = ModelGateway(db=object(), cred_repo=None, retry_config=None)  # type: ignore[arg-type]
        circuit = _ProbingCircuit(probe_free=True)
        reached_the_provider = False

        async def _no_client(*_a: object, **_k: object) -> None:
            nonlocal reached_the_provider
            reached_the_provider = True
            raise ProviderUnavailableError("stop here")

        with (
            patch("mail_verdict.pipeline.context.CircuitBreaker", return_value=circuit),
            patch("mail_verdict.core.structured_llm.resolve_client", _no_client),
            pytest.raises(StageUnavailable),
        ):
            await self._call(gateway)  # type: ignore[misc]
        assert reached_the_provider

    @pytest.mark.asyncio
    async def test_every_other_caller_is_turned_away(self) -> None:
        gateway = ModelGateway(db=object(), cred_repo=None, retry_config=None)  # type: ignore[arg-type]
        circuit = _ProbingCircuit(probe_free=False)

        async def _no_client(*_a: object, **_k: object) -> None:
            raise AssertionError("must not reach the provider")

        with (
            patch("mail_verdict.pipeline.context.CircuitBreaker", return_value=circuit),
            patch("mail_verdict.core.structured_llm.resolve_client", _no_client),
            pytest.raises(StageUnavailable, match="circuit suspended"),
        ):
            await self._call(gateway)  # type: ignore[misc]
