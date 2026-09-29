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

import pytest

from mail_verdict.pipeline.context import ModelGateway
from mail_verdict.pipeline.contracts import StageThrottled, StageTransient, StageUnavailable


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
