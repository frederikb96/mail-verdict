"""
Spam Analyst: LLM-based spam classification.

Abstract SpamAnalyst ABC with a LiveSpamAnalyst implementation that reads
the ai/spam/retry settings fresh on every call -- provider, model, and
reasoning effort are never captured at construction time, so a settings
change through the API takes effect on the very next message with no
restart. A keyword-only FakeSpamAnalyst is the test workhorse and the
"fake" provider option for API-key-free local development.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mail_verdict.core.prompts import load_static_prompt, render_prompt
from mail_verdict.core.retry import RetryConfig
from mail_verdict.core.structured_llm import (
    call_anthropic_structured,
    call_openai_structured,
    resolve_client,
)

if TYPE_CHECKING:
    from mail_verdict.settings.credentials import ProviderCredentialRepository
    from mail_verdict.settings.service import SettingsService

logger = logging.getLogger(__name__)

_VALID_VERDICTS = {"spam", "not-spam"}
MAX_REASONING_LENGTH = 200

SPAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": sorted(_VALID_VERDICTS)},
        "reasoning": {"type": "string", "maxLength": MAX_REASONING_LENGTH},
    },
    "required": ["verdict", "reasoning"],
    "additionalProperties": False,
}

_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")


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
    reasoning: str = ""
    raw_response: dict[str, Any] | None = None


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
        context: Full analysis context for the message
    """
    context_json = json.dumps(context.to_dict(), indent=2, ensure_ascii=False)
    if len(context_json) > MAX_CONTENT_LENGTH:
        context_json = context_json[:MAX_CONTENT_LENGTH] + "\n... [truncated]"
    return render_prompt("spam_user.md.j2", context_json=context_json)


def _looks_like_one_sentence(text: str) -> bool:
    """True if text has at most one sentence-ending punctuation mark."""
    return len(_SENTENCE_END_RE.findall(text.strip())) <= 1


def _validate_spam_shape(data: dict[str, Any]) -> None:
    """
    Validate a parsed spam verdict response.

    Defense in depth: the provider's schema enforcement should already
    guarantee this shape, but a response that violates it anyway is a
    validation failure to retry, not something to trim silently.

    Args:
        data: Parsed JSON response

    Raises:
        ValueError: If the verdict or reasoning is invalid
    """
    verdict = data.get("verdict")
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"Invalid verdict {verdict!r}, expected one of {_VALID_VERDICTS}")
    reasoning = data.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning:
        raise ValueError("Missing or empty 'reasoning' in response")
    if not _looks_like_one_sentence(reasoning):
        raise ValueError("'reasoning' must be a single sentence")


class SpamAnalyst(ABC):
    """Abstract base for spam classification."""

    @abstractmethod
    async def analyze(self, context: AnalysisContext) -> SpamVerdict:
        """
        Analyze an email for spam.

        Args:
            context: Message envelope, excerpt and auth signals

        Returns:
            SpamVerdict with binary classification

        Raises:
            RuntimeError: If analysis fails after retries
        """


class LiveSpamAnalyst(SpamAnalyst):
    """
    Spam analyst that reads ai/spam/retry settings fresh on every call.

    The only analyst the pipeline ever constructs. Provider, model, and
    reasoning effort are read from SettingsService.get() at the moment of
    each analyze() call rather than captured once -- a provider switch or
    a model change through the settings API changes what the very next
    message is classified with.
    """

    def __init__(
        self,
        settings_service: SettingsService,
        cred_repo: ProviderCredentialRepository,
    ) -> None:
        """
        Initialize the live spam analyst.

        Args:
            settings_service: Application settings service
            cred_repo: Provider API key repository
        """
        self._settings = settings_service
        self._cred_repo = cred_repo
        self._system_prompt = _load_system_prompt()

    async def analyze(self, context: AnalysisContext) -> SpamVerdict:
        """
        Analyze an email using the currently configured provider.

        Args:
            context: Mail content and metadata

        Returns:
            SpamVerdict with binary classification

        Raises:
            ProviderUnavailableError: If the configured provider has no API key
            RuntimeError: If all retries are exhausted
            ValueError: If ai.provider is not a recognized value
        """
        ai_settings = self._settings.get("ai")
        provider = str(ai_settings.get("provider", "openai")).lower()

        if provider == "fake":
            return await FakeSpamAnalyst().analyze(context)

        retry_config = RetryConfig.from_settings(self._settings.get("retry"))
        client = await resolve_client(provider, self._cred_repo)
        model = str(ai_settings.get("model", ""))
        effort = ai_settings.get("reasoning_effort") or None
        max_tokens = int(ai_settings.get("max_tokens", 1024))
        user_prompt = _build_user_prompt(context)

        logger.debug(
            "Spam analysis prompt",
            extra={
                "mail_id": context.mail_id,
                "provider": provider,
                "model": model,
                "system_prompt": self._system_prompt,
                "user_prompt": user_prompt,
            },
        )

        if provider == "anthropic":
            data = await call_anthropic_structured(
                client, model, effort, max_tokens,
                self._system_prompt, user_prompt, SPAM_SCHEMA,
                retry_config, validate=_validate_spam_shape,
            )
        elif provider == "openai":
            data = await call_openai_structured(
                client, model, effort, max_tokens, "spam_verdict",
                self._system_prompt, user_prompt, SPAM_SCHEMA,
                retry_config, validate=_validate_spam_shape,
            )
        else:
            raise ValueError(f"Unknown ai.provider {provider!r}")

        logger.info(
            "Spam analysis complete",
            extra={"mail_id": context.mail_id, "verdict": data["verdict"], "model": model},
        )
        return SpamVerdict(
            is_spam=data["verdict"] == "spam", reasoning=data["reasoning"], raw_response=data,
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
        matched = next((kw for kw in self._keywords if kw in haystack), None)
        is_spam = matched is not None
        if matched:
            reasoning = f"Matched configured keyword '{matched}'."
        else:
            reasoning = "No configured keyword matched."
        return SpamVerdict(
            is_spam=is_spam,
            reasoning=reasoning,
            raw_response={"verdict": "spam" if is_spam else "not-spam"},
        )
