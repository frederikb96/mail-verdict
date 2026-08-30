"""
Per-rule AI enrichment via LLM classification.

Custom prompt + tag list -> LLM -> validated tag output. Reads the ai/retry
settings fresh on every run(), through the same provider dispatch and
schema enforcement the spam analyst uses, so a provider or model switch
takes effect on the next rule evaluation with no restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mail_verdict.core.prompts import render_prompt
from mail_verdict.core.retry import RetryConfig
from mail_verdict.core.structured_llm import (
    call_anthropic_structured,
    call_openai_structured,
    resolve_client,
)
from mail_verdict.rules.conditions import MailContext

if TYPE_CHECKING:
    from mail_verdict.settings.credentials import ProviderCredentialRepository
    from mail_verdict.settings.service import SettingsService

logger = logging.getLogger(__name__)

MAX_ENRICHMENT_CONTENT_LENGTH = 5_000
MAX_REASONING_LENGTH = 200


@dataclass
class EnrichmentResult:
    """Result of AI enrichment for a rule."""

    tags: list[str] = field(default_factory=list)
    reasoning: str = ""
    success: bool = True
    error: str | None = None


@dataclass
class EnrichmentConfig:
    """Per-rule enrichment configuration from YAML."""

    enabled: bool = False
    prompt: str = ""
    tags: list[str] = field(default_factory=list)


def _build_schema(allowed_tags: list[str]) -> dict[str, Any]:
    """Build a strict JSON schema constraining tags to the rule's allowed list."""
    return {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string", "enum": allowed_tags},
            },
            "reasoning": {"type": "string", "maxLength": MAX_REASONING_LENGTH},
        },
        "required": ["tags", "reasoning"],
        "additionalProperties": False,
    }


def _validate_enrichment_shape(data: dict[str, Any]) -> None:
    """
    Validate a parsed enrichment response.

    Args:
        data: Parsed JSON response

    Raises:
        ValueError: If tags or reasoning is missing or malformed
    """
    tags = data.get("tags")
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValueError("'tags' must be a list of strings")
    if not isinstance(data.get("reasoning"), str):
        raise ValueError("Missing 'reasoning' in response")


class EnrichmentRunner:
    """
    Runs AI enrichment for a single rule's config.

    Builds prompt from rule config + mail context, calls the currently
    configured provider under a strict tag schema, validates output
    against the rule's allowed tag list.
    """

    def __init__(
        self,
        settings_service: SettingsService,
        cred_repo: ProviderCredentialRepository,
        excerpt_length: int = 500,
    ) -> None:
        """
        Initialize enrichment runner.

        Args:
            settings_service: Application settings service
            cred_repo: Provider API key repository
            excerpt_length: Max chars of body to include in prompt
        """
        self._settings = settings_service
        self._cred_repo = cred_repo
        self._excerpt_length = excerpt_length

    async def run(
        self,
        config: EnrichmentConfig,
        ctx: MailContext,
    ) -> EnrichmentResult:
        """
        Run AI enrichment for a rule.

        Args:
            config: Enrichment config from the rule
            ctx: Mail context with email data

        Returns:
            EnrichmentResult with tags and reasoning
        """
        if not config.enabled or not config.tags:
            return EnrichmentResult(success=True)

        tag_list_str = ", ".join(config.tags)
        system_prompt = render_prompt(
            "enrichment_system.md.j2",
            tag_list=tag_list_str,
        )

        max_len = min(self._excerpt_length, MAX_ENRICHMENT_CONTENT_LENGTH)
        body_excerpt = ctx.body_text[:max_len] if ctx.body_text else ""
        user_prompt = render_prompt(
            "enrichment_user.md.j2",
            custom_prompt=config.prompt,
            from_addr=ctx.from_addr or "",
            subject=ctx.subject or "",
            body_excerpt=body_excerpt,
        )

        try:
            data = await self._call_provider(system_prompt, user_prompt, config.tags)
        except Exception as exc:
            logger.error("Enrichment LLM call failed", extra={"error": str(exc)})
            return EnrichmentResult(success=False, error=str(exc))

        return EnrichmentResult(
            tags=list(data["tags"]), reasoning=str(data["reasoning"]), success=True,
        )

    async def _call_provider(
        self, system_prompt: str, user_prompt: str, allowed_tags: list[str],
    ) -> dict[str, Any]:
        """Dispatch to the currently configured provider under a strict tag schema."""
        ai_settings = self._settings.get("ai")
        provider = str(ai_settings.get("provider", "openai")).lower()
        retry_config = RetryConfig.from_settings(self._settings.get("retry"))
        client = await resolve_client(provider, self._cred_repo)
        model = str(ai_settings.get("enrichment_model") or ai_settings.get("model", ""))
        effort = ai_settings.get("reasoning_effort") or None
        max_tokens = 256
        schema = _build_schema(allowed_tags)

        logger.debug(
            "Enrichment prompt",
            extra={
                "provider": provider, "model": model,
                "system_prompt": system_prompt, "user_prompt": user_prompt,
            },
        )

        if provider == "anthropic":
            return await call_anthropic_structured(
                client, model, effort, max_tokens, system_prompt, user_prompt, schema,
                retry_config, validate=_validate_enrichment_shape,
            )
        if provider == "openai":
            return await call_openai_structured(
                client, model, effort, max_tokens, "enrichment_tags",
                system_prompt, user_prompt, schema,
                retry_config, validate=_validate_enrichment_shape,
            )
        raise ValueError(f"Unknown ai.provider {provider!r}")
