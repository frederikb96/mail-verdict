"""
Spam Analyst: LLM-based spam classification.

Abstract SpamAnalyst ABC with OpenAI implementation.
Takes mail context + neighbor data, returns binary spam/not-spam verdict.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from mail_verdict.core.prompts import load_static_prompt, render_prompt
from mail_verdict.core.retry import RetryConfig

logger = logging.getLogger(__name__)

_VALID_VERDICTS = {"spam", "not-spam"}


@dataclass
class NeighborContext:
    """Context for a single neighbor mail used in spam analysis."""

    mail_id: str
    tag: str | None
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for prompt construction."""
        return {
            "mail_id": self.mail_id,
            "tag": self.tag or "unknown",
            "excerpt": self.excerpt,
        }


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
    neighbors: list[NeighborContext] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for the LLM prompt."""
        result: dict[str, Any] = {
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
            "neighbors": [n.to_dict() for n in self.neighbors],
        }
        return result


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


class OpenAISpamAnalyst(SpamAnalyst):
    """Spam analyst using OpenAI chat completions with JSON mode."""

    def __init__(
        self,
        ai_settings: dict[str, Any],
        spam_settings: dict[str, Any],
        retry_config: RetryConfig,
    ) -> None:
        """
        Initialize the OpenAI spam analyst.

        Args:
            ai_settings: AI settings dict (model key)
            spam_settings: Spam settings dict
            retry_config: Retry configuration
        """
        self._model = ai_settings.get("model", "gpt-5-mini")
        self._reasoning_effort = ai_settings.get("reasoning_effort")
        self._retry = retry_config
        self._system_prompt = _load_system_prompt()

    def _get_client(self) -> Any:
        """Get the OpenAI client from the global provider."""
        from mail_verdict.core.openai_provider import get_openai_client

        client = get_openai_client()
        if client is None:
            raise RuntimeError("No OpenAI API key configured")
        return client

    async def analyze(self, context: AnalysisContext) -> SpamVerdict:
        """
        Analyze an email for spam using OpenAI chat completions.

        Retries on malformed responses with exponential backoff.

        Args:
            context: Mail content and neighbor context

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
                create_kwargs: dict[str, Any] = {
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": self._system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "response_format": {"type": "json_object"},
                }
                if self._reasoning_effort:
                    create_kwargs["reasoning"] = {"effort": self._reasoning_effort}

                response = await client.chat.completions.create(**create_kwargs)

                raw_content = response.choices[0].message.content or ""
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
                from openai import RateLimitError

                last_error = e
                if isinstance(e, RateLimitError):
                    retry_after = getattr(e, "retry_after", None)
                    delay = (
                        float(retry_after) if retry_after
                        else self._retry.delay_for_attempt(attempt)
                    )
                    logger.warning(
                        "OpenAI rate limited, backing off",
                        extra={
                            "mail_id": context.mail_id,
                            "delay": delay,
                        },
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
