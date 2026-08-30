"""
Provider-agnostic strict-schema LLM completion.

The one place a classification or enrichment request actually leaves the
process: resolves the configured provider's client, issues the request
under a JSON schema the provider enforces server-side (Anthropic's
`output_config.format`, OpenAI's `text.format` with `strict: true`), and
retries transient failures with full-jitter exponential backoff. A
response that violates the schema is treated the same as a transient
failure -- retried, never trimmed or accepted partially.

Callers never see a raw client: resolve_client() raises
ProviderUnavailableError for the one case worth telling apart from a
retryable failure -- there is no key to call with at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from mail_verdict.core.errors import ProviderUnavailableError
from mail_verdict.core.retry import RetryConfig

if TYPE_CHECKING:
    from mail_verdict.settings.credentials import ProviderCredentialRepository

logger = logging.getLogger(__name__)


async def resolve_client(provider: str, cred_repo: ProviderCredentialRepository) -> Any:
    """
    Resolve a live client for the given provider, reading its key fresh.

    Args:
        provider: "anthropic" or "openai"
        cred_repo: Provider credential repository

    Returns:
        A provider client

    Raises:
        ProviderUnavailableError: If no API key is configured
        ValueError: If the provider name is not one this module supports
    """
    api_key = await cred_repo.resolve_key(provider)
    if not api_key:
        raise ProviderUnavailableError(f"No {provider} API key configured")

    if provider == "anthropic":
        from mail_verdict.core.anthropic_provider import get_anthropic_client

        return get_anthropic_client(api_key)
    if provider == "openai":
        from mail_verdict.core.openai_provider import get_openai_client

        return get_openai_client(api_key)
    raise ValueError(f"Unsupported provider {provider!r}")


async def retry_structured_call(
    call_once: Callable[[], Awaitable[str]],
    retry_config: RetryConfig,
    transient_errors: tuple[type[Exception], ...],
    validate: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """
    Call, parse, and validate a strict-JSON-schema response, retrying transient failures.

    Malformed JSON, a schema-shape violation caught by `validate`, and
    anything in `transient_errors` (rate limits, connection drops, server
    errors) are retried with full-jitter backoff. Any other exception --
    a bad request, an auth failure, an unknown model -- propagates
    immediately: retrying it would only mask a real bug.

    Args:
        call_once: Issues one request and returns the raw text response
        retry_config: Backoff parameters
        transient_errors: Exception types worth retrying, beyond parse/validate failures
        validate: Optional extra validation on the parsed dict, raising
            ValueError on a violation

    Returns:
        The parsed response dict

    Raises:
        RuntimeError: If every attempt failed
    """
    last_error: Exception | None = None

    for attempt in range(retry_config.max_retries + 1):
        try:
            raw = await call_once()
            data: dict[str, Any] = json.loads(raw)
            if validate is not None:
                validate(data)
            return data
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
        except transient_errors as exc:
            last_error = exc

        if attempt < retry_config.max_retries:
            delay = retry_config.delay_for_attempt(attempt)
            logger.warning(
                "LLM structured call failed, retrying",
                extra={"attempt": attempt + 1, "delay": delay, "error": str(last_error)},
            )
            await asyncio.sleep(delay)

    raise RuntimeError(
        f"LLM structured call failed after {retry_config.max_retries + 1} attempts: {last_error}"
    )


async def call_anthropic_structured(
    client: Any,
    model: str,
    effort: str | None,
    max_tokens: int,
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    retry_config: RetryConfig,
    validate: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Issue a strict-schema request against the Anthropic Messages API."""
    from anthropic import APIConnectionError, InternalServerError, RateLimitError

    async def _call_once() -> str:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if effort:
            output_config["effort"] = effort
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            output_config=output_config,
        )
        return "".join(block.text for block in response.content if block.type == "text")

    return await retry_structured_call(
        _call_once,
        retry_config,
        transient_errors=(RateLimitError, APIConnectionError, InternalServerError),
        validate=validate,
    )


async def call_openai_structured(
    client: Any,
    model: str,
    effort: str | None,
    max_tokens: int,
    schema_name: str,
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    retry_config: RetryConfig,
    validate: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Issue a strict-schema request against the OpenAI Responses API."""
    from openai import APIConnectionError, InternalServerError, RateLimitError

    async def _call_once() -> str:
        kwargs: dict[str, Any] = {}
        if effort and effort != "none":
            kwargs["reasoning"] = {"effort": effort}
        if max_tokens:
            kwargs["max_output_tokens"] = max_tokens
        response = await client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": schema,
                    "strict": True,
                },
            },
            **kwargs,
        )
        return str(response.output_text)

    return await retry_structured_call(
        _call_once,
        retry_config,
        transient_errors=(RateLimitError, APIConnectionError, InternalServerError),
        validate=validate,
    )
