"""
Per-rule AI enrichment via LLM classification.

Custom prompt + tag list -> LLM -> validated tag output.
Uses the project's existing AI config (OpenAI-compatible).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from mail_verdict.core.prompts import render_prompt
from mail_verdict.rules.conditions import MailContext

logger = logging.getLogger(__name__)

MAX_ENRICHMENT_CONTENT_LENGTH = 5_000


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


class EnrichmentRunner:
    """
    Runs AI enrichment for a single rule's config.

    Builds prompt from rule config + mail context, calls LLM,
    validates output against allowed tag list.
    """

    def __init__(
        self,
        ai_provider: str,
        ai_model: str,
        max_retries: int = 2,
        excerpt_length: int = 500,
        reasoning_effort: str | None = None,
    ) -> None:
        """
        Initialize enrichment runner.

        Args:
            ai_provider: AI provider name (e.g. "openai")
            ai_model: Model identifier from config
            max_retries: Retries on malformed LLM output
            excerpt_length: Max chars of body to include in prompt
            reasoning_effort: OpenAI reasoning effort level (minimal/low/medium/high)
        """
        self._provider = ai_provider
        self._model = ai_model
        self._max_retries = max_retries
        self._excerpt_length = excerpt_length
        self._reasoning_effort = reasoning_effort

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

        for attempt in range(self._max_retries + 1):
            try:
                raw_response = await self._call_llm(system_prompt, user_prompt)
                result = self._parse_and_validate(raw_response, config.tags)
                return result
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                if attempt < self._max_retries:
                    logger.warning(
                        "Enrichment LLM returned malformed output, retrying",
                        extra={"attempt": attempt + 1, "error": str(exc)},
                    )
                    continue
                logger.error(
                    "Enrichment failed after retries",
                    extra={"error": str(exc)},
                )
                return EnrichmentResult(
                    success=False,
                    error=f"Malformed LLM output: {exc}",
                )
            except Exception as exc:
                logger.error(
                    "Enrichment LLM call failed",
                    extra={"error": str(exc)},
                )
                return EnrichmentResult(
                    success=False,
                    error=str(exc),
                )

        return EnrichmentResult(success=False, error="Max retries exceeded")

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        """
        Call the LLM API and return raw response text.

        Args:
            system_prompt: System message for the LLM
            user_prompt: User message with email context

        Returns:
            Raw response text from the LLM
        """
        from mail_verdict.core.openai_provider import get_openai_client

        client = get_openai_client()
        if client is None:
            raise RuntimeError("No OpenAI API key configured")

        logger.debug(
            "Enrichment prompt",
            extra={
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "model": self._model,
            },
        )

        create_kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 256,
        }
        if self._reasoning_effort:
            create_kwargs["reasoning"] = {"effort": self._reasoning_effort}

        response = await client.chat.completions.create(**create_kwargs)

        content = response.choices[0].message.content
        if content is None:
            raise ValueError("LLM returned empty response")
        return content

    def _parse_and_validate(
        self,
        raw: str,
        allowed_tags: list[str],
    ) -> EnrichmentResult:
        """
        Parse LLM JSON output and validate tags against allowed list.

        Args:
            raw: Raw LLM response string
            allowed_tags: Tags allowed for this rule

        Returns:
            Validated EnrichmentResult

        Raises:
            json.JSONDecodeError: If output is not valid JSON
            KeyError: If required keys are missing
            ValueError: If tags contain invalid values
        """
        # Strip markdown fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            cleaned = "\n".join(lines)

        data: dict[str, Any] = json.loads(cleaned)

        if "tags" not in data:
            raise KeyError("Missing 'tags' key in LLM response")

        raw_tags: list[str] = data["tags"]
        if not isinstance(raw_tags, list):
            raise ValueError(f"Expected list for 'tags', got {type(raw_tags)}")

        allowed_lower = {t.lower(): t for t in allowed_tags}
        validated_tags: list[str] = []
        for tag in raw_tags:
            tag_lower = tag.lower()
            if tag_lower in allowed_lower:
                validated_tags.append(allowed_lower[tag_lower])
            else:
                logger.warning(
                    "Enrichment returned tag not in allowed list",
                    extra={"tag": tag, "allowed": allowed_tags},
                )

        reasoning = data.get("reasoning", "")
        return EnrichmentResult(
            tags=validated_tags,
            reasoning=str(reasoning),
            success=True,
        )
