"""
Provider-agnostic strict-schema LLM completion.

The one place a classification or enrichment request actually leaves the
process: resolves the configured provider's client, issues the request
under a JSON schema the provider enforces server-side (Anthropic's
`output_config.format`, OpenAI's `text.format` with `strict: true`, a
"custom" compatible server's `response_format.json_schema`), and retries
transient failures with full-jitter exponential backoff. A response that
violates the schema is treated the same as a transient failure -- retried,
never trimmed or accepted partially.

A "custom" provider -- any OpenAI-compatible server reached at a
category's own base_url, Infomaniak among them -- speaks chat completions
only, not OpenAI's own Responses API `call_openai_structured` uses:
`call_chat_completions_structured` is the separate request shape that
gap needs, over the same `AsyncOpenAI` client class (core/openai_provider.py
points it at the compatible server's base_url instead of api.openai.com).
`json_object` response formatting is commonly rejected by such a server in
favour of a declared `json_schema` -- this module never emits the former.

Callers never see a raw client: resolve_client() raises
ProviderUnavailableError for the one case worth telling apart from a
retryable failure -- there is no key to call with at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from mail_verdict.core.errors import ProviderUnavailableError
from mail_verdict.core.retry import RetryConfig

if TYPE_CHECKING:
    from mail_verdict.settings.credentials import ProviderCredentialRepository

logger = logging.getLogger(__name__)


def retry_after_from_exception(exc: BaseException, *, default: timedelta) -> timedelta:
    """
    How long a 429 actually asked callers to wait, read from the
    response's own `Retry-After` header -- rather than a blind guess.
    `openai.RateLimitError` (shared by real OpenAI and any "custom"
    OpenAI-compatible server, since both raise the same SDK exception
    class keyed on HTTP status) carries no parsed retry-delay attribute
    of its own, only the raw `httpx.Response` on `.response`.

    Args:
        exc: The caught exception -- anything without a `.response.headers`
            (a connection error, a wrapped/re-raised exception with no
            response attached) falls through to `default`
        default: Used when no header is present, or it doesn't parse as
            a plain integer/float seconds count (an HTTP-date form is
            valid per RFC 9110 but not handled here -- rare for a JSON
            API, and the safe default covers it)

    Returns:
        The server's requested delay, or `default`
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    header = headers.get("retry-after") if headers is not None else None
    if header:
        try:
            return timedelta(seconds=float(header))
        except ValueError:
            pass
    return default


async def resolve_client(
    provider: str, cred_repo: ProviderCredentialRepository, *, base_url: str | None = None,
) -> Any:
    """
    Resolve a live client for the given provider, reading its key fresh.

    Args:
        provider: "anthropic", "openai" or "custom"
        cred_repo: Provider credential repository
        base_url: The compatible server's API base -- required for
            "custom", ignored otherwise. Validated as present at settings
            write time (settings/ai_validation.py); this is defence in
            depth for a caller that bypasses that write path.

    Returns:
        A provider client

    Raises:
        ProviderUnavailableError: If no API key is configured, or provider
            is "custom" with no base_url
        ValueError: If the provider name is not one this module supports
    """
    if provider == "custom" and not base_url:
        raise ProviderUnavailableError("custom provider has no base_url configured")

    api_key = await cred_repo.resolve_key(provider)
    if not api_key:
        raise ProviderUnavailableError(f"No {provider} API key configured")

    if provider == "anthropic":
        from mail_verdict.core.anthropic_provider import get_anthropic_client

        return get_anthropic_client(api_key)
    if provider in ("openai", "custom"):
        from mail_verdict.core.openai_provider import get_openai_client

        return get_openai_client(api_key, base_url=base_url if provider == "custom" else None)
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
        RuntimeError: If every attempt failed -- chained from the last
            underlying error (`raise ... from last_error`) so a caller
            that needs to know *what kind* of failure this was (a
            sustained rate limit, say, which must never count against a
            retry budget the way an ordinary transient failure does) can
            still recover it via `__cause__` rather than losing it to a
            generic wrapper type.
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
    ) from last_error


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
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Issue a strict-schema request against the Anthropic Messages API.

    timeout_seconds overrides the client's own per-request timeout
    (anthropic_provider.py's REQUEST_TIMEOUT_SECONDS) for this call alone,
    when a caller's own budget for a full-length response needs more room
    than the shared default allows. Left unset, the client's default holds.
    """
    from anthropic import APIConnectionError, InternalServerError, RateLimitError

    async def _call_once() -> str:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if effort:
            output_config["effort"] = effort
        kwargs: dict[str, Any] = {}
        if timeout_seconds is not None:
            kwargs["timeout"] = timeout_seconds
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            output_config=output_config,
            **kwargs,
        )
        return "".join(block.text for block in response.content if block.type == "text")

    return await retry_structured_call(
        _call_once,
        retry_config,
        transient_errors=(RateLimitError, APIConnectionError, InternalServerError),
        validate=validate,
    )


async def call_chat_completions_structured(
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
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """
    Issue a strict-schema request against an OpenAI-compatible Chat
    Completions endpoint -- the request shape a "custom" provider needs,
    since such a server (Infomaniak among them) serves chat completions
    only and rejects OpenAI's own Responses API.

    `response_format.json_schema` is Chat Completions' structured-output
    field, distinct from the Responses API's `text.format`; `reasoning_effort`
    is passed via `extra_body` rather than the SDK's own typed kwarg, since
    a compatible server's accepted request shape for it is otherwise
    unverified per model. A response whose `content` comes back `None` --
    a reasoning model that exhausted its output budget on thinking, see
    the module docstring -- becomes an empty string here so it fails
    `json.loads` and is retried like any other malformed response, rather
    than raising a TypeError that would not be.

    "none" is sent explicitly, not omitted: a compatible server's
    reasoning models reason by default, so an absent field means "reason"
    to them, not "don't" -- measured directly against Infomaniak's own
    Qwen3.5-122B-A10B-FP8, which spent 2,000+ hidden reasoning tokens and
    15-20 real seconds per call with the field left out, and answered in
    under a second with it sent as `"none"`. Only a genuinely unset
    effort (`None`) is left off the request.

    timeout_seconds overrides the client's own per-request timeout
    (openai_provider.py's REQUEST_TIMEOUT_SECONDS, sized for classify and
    embeddings' tighter leases) for this call alone, when a caller's own
    budget for a full-length response needs more room. Left unset, the
    client's default holds.
    """
    from openai import APIConnectionError, InternalServerError, RateLimitError

    async def _call_once() -> str:
        kwargs: dict[str, Any] = {}
        if effort:
            kwargs["extra_body"] = {"reasoning_effort": effort}
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if timeout_seconds is not None:
            kwargs["timeout"] = timeout_seconds
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            },
            **kwargs,
        )
        content = response.choices[0].message.content
        return content if content is not None else ""

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
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Issue a strict-schema request against the OpenAI Responses API.

    timeout_seconds overrides the client's own per-request timeout for
    this call alone; see call_chat_completions_structured's docstring.
    """
    from openai import APIConnectionError, InternalServerError, RateLimitError

    async def _call_once() -> str:
        kwargs: dict[str, Any] = {}
        if effort and effort != "none":
            kwargs["reasoning"] = {"effort": effort}
        if max_tokens:
            kwargs["max_output_tokens"] = max_tokens
        if timeout_seconds is not None:
            kwargs["timeout"] = timeout_seconds
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
