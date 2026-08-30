"""
Spam Analyst: LLM-based spam classification.

Abstract SpamAnalyst ABC with an Anthropic implementation. Takes mail
context, returns a binary spam/not-spam verdict.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from mail_verdict.core.prompts import load_static_prompt, render_prompt
from mail_verdict.core.retry import RetryConfig

logger = logging.getLogger(__name__)

_VALID_VERDICTS = {"spam", "not-spam"}


@dataclass
class AnalysisContext:
    """Full context passed to the spam analyst."""

    mail_id: str
    from_addr: str | None
    to_addrs: str | None
    subject: str | None
    body_excerpt: str
    dkim_pass: bool | None = None
    spf_pass: bool | None = None
    dmarc_pass: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for the LLM prompt."""
        return {
            "new_mail": {
                "from": self.from_addr or "",
                "to": self.to_addrs or "",
                "subject": self.subject or "",
                "body_excerpt": self.body_excerpt,
                "auth": {
                    "dkim": _auth_str(self.dkim_pass),
                    "spf": _auth_str(self.spf_pass),
                    "dmarc": _auth_str(self.dmarc_pass),
                },
            },
        }


@dataclass
class SpamVerdict:
    """Result of spam analysis."""

    is_spam: bool
    raw_response: dict[str, Any]


def _auth_str(value: bool | None) -> str:
    """Convert auth boolean to display string."""
    if value is None:
        return "unknown"
    return "pass" if value else "fail"


def _load_system_prompt() -> str:
    """
    Load the spam analyst system prompt from Jinja2 template.

    Returns:
        Rendered system prompt string

    Raises:
        jinja2.TemplateNotFound: If template file not found
    """
    return load_static_prompt("spam_system.md.j2")


MAX_CONTENT_LENGTH = 10_000


def _build_user_prompt(context: AnalysisContext) -> str:
    """
    Build the user prompt from analysis context via Jinja2 template.

    Args:
        context: Full analysis context with mail + neighbors
    """
    context_json = json.dumps(context.to_dict(), indent=2, ensure_ascii=False)
    if len(context_json) > MAX_CONTENT_LENGTH:
        context_json = context_json[:MAX_CONTENT_LENGTH] + "\n... [truncated]"
    return render_prompt("spam_user.md.j2", context_json=context_json)


def _parse_verdict(raw: str) -> SpamVerdict:
    """
    Parse LLM response into SpamVerdict.

    Args:
        raw: Raw JSON string from LLM

    Raises:
        ValueError: If response is malformed or verdict is invalid
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Response is not valid JSON: {e}") from e

    verdict_str = data.get("verdict")
    if verdict_str not in _VALID_VERDICTS:
        raise ValueError(f"Invalid verdict '{verdict_str}', expected one of {_VALID_VERDICTS}")

    return SpamVerdict(
        is_spam=(verdict_str == "spam"),
        raw_response=data,
    )


class SpamAnalyst(ABC):
    """Abstract base for spam classification via LLM."""

    @abstractmethod
    async def analyze(self, context: AnalysisContext) -> SpamVerdict:
        """
        Analyze an email for spam.

        Args:
            context: Mail content and neighbor context

        Returns:
            SpamVerdict with binary classification

        Raises:
            RuntimeError: If analysis fails after retries
        """


class AnthropicSpamAnalyst(SpamAnalyst):
    """Spam analyst using the Anthropic Messages API."""

    def __init__(
        self,
        ai_settings: dict[str, Any],
        spam_settings: dict[str, Any],
        retry_config: RetryConfig,
    ) -> None:
        """
        Initialize the Anthropic spam analyst.

        Args:
            ai_settings: AI settings dict (model, max_tokens keys)
            spam_settings: Spam settings dict
            retry_config: Retry configuration
        """
        self._model = ai_settings.get("model", "claude-haiku-4-5")
        self._max_tokens = int(ai_settings.get("max_tokens", 1024))
        self._retry = retry_config
        self._system_prompt = _load_system_prompt()

    def _get_client(self) -> Any:
        """Get the Anthropic client from the global provider."""
        from mail_verdict.core.anthropic_provider import get_anthropic_client

        client = get_anthropic_client()
        if client is None:
            raise RuntimeError("No Anthropic API key configured")
        return client

    async def analyze(self, context: AnalysisContext) -> SpamVerdict:
        """
        Analyze an email for spam using the Anthropic Messages API.

        Retries on malformed responses and rate limits with exponential
        backoff.

        Args:
            context: Mail content and metadata

        Returns:
            SpamVerdict with binary classification

        Raises:
            RuntimeError: If all retries exhausted
        """
        user_prompt = _build_user_prompt(context)
        client = self._get_client()
        last_error: Exception | None = None

        logger.debug(
            "Spam analysis prompt",
            extra={
                "mail_id": context.mail_id,
                "system_prompt": self._system_prompt,
                "user_prompt": user_prompt,
                "model": self._model,
            },
        )

        for attempt in range(self._retry.max_retries + 1):
            try:
                response = await client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=self._system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                )

                raw_content = "".join(
                    block.text for block in response.content if block.type == "text"
                )
                verdict = _parse_verdict(raw_content)

                logger.info(
                    "Spam analysis complete",
                    extra={
                        "mail_id": context.mail_id,
                        "verdict": "spam" if verdict.is_spam else "not-spam",
                        "model": self._model,
                    },
                )
                return verdict

            except ValueError as e:
                last_error = e
                if attempt < self._retry.max_retries:
                    delay = self._retry.delay_for_attempt(attempt)
                    logger.warning(
                        "Malformed spam analysis response, retrying",
                        extra={
                            "mail_id": context.mail_id,
                            "attempt": attempt + 1,
                            "delay": delay,
                            "error": str(e),
                        },
                    )
                    await asyncio.sleep(delay)

            except Exception as e:
                from anthropic import RateLimitError

                last_error = e
                if isinstance(e, RateLimitError):
                    delay = self._retry.delay_for_attempt(attempt)
                    logger.warning(
                        "Anthropic rate limited, backing off",
                        extra={"mail_id": context.mail_id, "delay": delay},
                    )
                    await asyncio.sleep(delay)
                elif attempt < self._retry.max_retries:
                    delay = self._retry.delay_for_attempt(attempt)
                    logger.warning(
                        "Spam analysis API call failed, retrying",
                        extra={
                            "mail_id": context.mail_id,
                            "attempt": attempt + 1,
                            "delay": delay,
                            "error": str(e),
                        },
                    )
                    await asyncio.sleep(delay)

        raise RuntimeError(
            f"Spam analysis failed after {self._retry.max_retries + 1} attempts: {last_error}"
        )


class FakeSpamAnalyst(SpamAnalyst):
    """Deterministic, keyword-driven analyst -- the test workhorse.

    Never calls out to a real LLM: flags a message as spam if any of a
    configurable set of keywords appears (case-insensitive) in the subject
    or body excerpt. Used by tests and available as a settings-selectable
    provider for local development without an API key.
    """

    DEFAULT_KEYWORDS = ("viagra", "lottery winner", "wire transfer", "nigerian prince")

    def __init__(self, keywords: tuple[str, ...] = DEFAULT_KEYWORDS) -> None:
        """
        Initialize the fake analyst.

        Args:
            keywords: Lowercase substrings that trigger a spam verdict
        """
        self._keywords = keywords

    async def analyze(self, context: AnalysisContext) -> SpamVerdict:
        """
        Classify as spam if any configured keyword appears in the content.

        Args:
            context: Mail content and metadata

        Returns:
            SpamVerdict with a deterministic classification
        """
        haystack = f"{context.subject or ''} {context.body_excerpt}".lower()
        is_spam = any(kw in haystack for kw in self._keywords)
        return SpamVerdict(
            is_spam=is_spam,
            raw_response={"verdict": "spam" if is_spam else "not-spam"},
        )
