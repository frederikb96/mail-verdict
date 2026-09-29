"""
Validates the ai/semantic provider settings at write time: the
provider/reasoning_effort combination, and that a "custom" provider always
carries the base_url its client construction needs.

The two named vendors spell reasoning effort differently and support
different levels, so an invalid pairing is rejected here rather than
surfacing as a classification failure on the next inbound message.
"""

from __future__ import annotations

from typing import Any

KNOWN_PROVIDERS = frozenset({"anthropic", "openai", "custom", "fake"})

# No embedding model of Anthropic's own to select here, unlike ai.provider.
SEMANTIC_KNOWN_PROVIDERS = frozenset({"openai", "custom", "fake"})

# Anthropic's output_config.effort levels (Messages API structured outputs).
ANTHROPIC_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})

# OpenAI's reasoning.effort levels across the gpt-5 family. Which subset a
# given model actually accepts varies -- gpt-5.4-nano rejects "minimal"
# with a 400 naming its five accepted values (none/low/medium/high/xhigh),
# while "minimal" is documented for other gpt-5 variants. This is the union
# across the family, checked at the provider level; an unsupported level
# for one specific model still surfaces as a clear provider error rather
# than a validation failure here.
OPENAI_EFFORT_LEVELS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})

# "custom" covers any OpenAI-compatible server, and which reasoning_effort
# values (if any) a given deployment's model accepts is unknowable here --
# unlike the two named vendors above, there is no fixed family to check
# against. Skipped entirely, the same as "fake": a bad value still surfaces
# as a clear provider error on the next call, rather than a guessed-wrong
# validation failure here.
_SKIP_EFFORT_CHECK = frozenset({"fake", "custom"})


def _require_base_url_for_custom(category: str, provider: str, effective: dict[str, Any]) -> None:
    if provider == "custom" and not effective.get("base_url"):
        raise ValueError(
            f"{category}.base_url is required when {category}.provider is 'custom'"
        )


def validate_ai_settings(effective: dict[str, Any]) -> None:
    """
    Validate a merged ai settings dict as it would read after a write.

    Args:
        effective: The ai settings dict with the incoming partial update
            already merged onto the existing values

    Raises:
        ValueError: If the provider is unknown, reasoning_effort is not a
            level the selected provider supports, or provider is "custom"
            with no base_url
    """
    provider = str(effective.get("provider", "")).lower()
    if provider not in KNOWN_PROVIDERS:
        raise ValueError(
            f"ai.provider must be one of {sorted(KNOWN_PROVIDERS)}, got {provider!r}"
        )
    _require_base_url_for_custom("ai", provider, effective)

    effort = effective.get("reasoning_effort")
    if effort is None or provider in _SKIP_EFFORT_CHECK:
        return

    levels = ANTHROPIC_EFFORT_LEVELS if provider == "anthropic" else OPENAI_EFFORT_LEVELS
    if str(effort).lower() not in levels:
        raise ValueError(
            f"ai.reasoning_effort {effort!r} is not valid for provider {provider!r}; "
            f"expected one of {sorted(levels)}"
        )


def validate_semantic_settings(effective: dict[str, Any]) -> None:
    """
    Validate a merged semantic settings dict as it would read after a write.

    Args:
        effective: The semantic settings dict with the incoming partial
            update already merged onto the existing values

    Raises:
        ValueError: If the provider is unknown, or it is "custom" with no
            base_url
    """
    provider = str(effective.get("provider", "")).lower()
    if provider not in SEMANTIC_KNOWN_PROVIDERS:
        raise ValueError(
            f"semantic.provider must be one of {sorted(SEMANTIC_KNOWN_PROVIDERS)}, "
            f"got {provider!r}"
        )
    _require_base_url_for_custom("semantic", provider, effective)
