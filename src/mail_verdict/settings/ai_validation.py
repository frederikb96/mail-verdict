"""
Validates the provider settings of every AI task at write time: the
provider/reasoning_effort combination, and that a "custom" provider always
carries the base_url its client construction needs. A task is a settings
category (ai, assistant, semantic) or a prefixed key group of one (orders'
decide_ and write_), all checked by validate_task_model.

The two named vendors spell reasoning effort differently and support
different levels, so an invalid pairing is rejected here rather than
surfacing as a classification failure on the next inbound message.
"""

from __future__ import annotations

from typing import Any

KNOWN_PROVIDERS = frozenset({"anthropic", "openai", "custom", "fake"})

# No embedding model of Anthropic's own to select here, unlike ai.provider.
SEMANTIC_KNOWN_PROVIDERS = frozenset({"openai", "custom", "fake"})

# The ModelGateway category of every AI task, which is also the category
# half of its circuit breaker name (`<provider>:<category>`).
AI_TASK_CATEGORIES = ("ai", "semantic", "assistant", "orders.decide", "orders.write")

# Anthropic's output_config.effort levels (Messages API structured outputs).
ANTHROPIC_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})

# OpenAI's reasoning.effort levels across the gpt-5 family. Which subset a
# given model actually accepts varies -- gpt-6-luna rejects "minimal"
# with a 400 naming its six accepted values (none/low/medium/high/xhigh/max),
# while "minimal" is documented for older gpt-5 variants. This is the union
# across the family, checked at the provider level; an unsupported level
# for one specific model still surfaces as a clear provider error rather
# than a validation failure here.
OPENAI_EFFORT_LEVELS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})

# "custom" covers any OpenAI-compatible server, and which reasoning_effort
# values (if any) a given deployment's model accepts is unknowable here --
# unlike the two named vendors above, there is no fixed family to check
# against. Skipped entirely, the same as "fake": a bad value still surfaces
# as a clear provider error on the next call, rather than a guessed-wrong
# validation failure here.
_SKIP_EFFORT_CHECK = frozenset({"fake", "custom"})


def validate_task_model(label: str, effective: dict[str, Any], *, prefix: str = "") -> None:
    """
    Validate one AI task's provider, server address and reasoning effort
    as they would read after a write.

    Args:
        label: How error messages name the task's keys, e.g. "ai." or
            "orders.decide_" -- the dotted category plus the key prefix
        effective: The category's settings dict with the incoming partial
            update already merged onto the existing values
        prefix: The key prefix inside `effective` ("decide_" for the
            orders decide task, "" for a category that is one task)

    Raises:
        ValueError: If the provider is unknown, reasoning_effort is not a
            level the selected provider supports, or provider is "custom"
            with no base_url
    """
    provider = str(effective.get(f"{prefix}provider", "")).lower()
    if provider not in KNOWN_PROVIDERS:
        raise ValueError(
            f"{label}provider must be one of {sorted(KNOWN_PROVIDERS)}, got {provider!r}"
        )
    if provider == "custom" and not effective.get(f"{prefix}base_url"):
        raise ValueError(f"{label}base_url is required when {label}provider is 'custom'")

    effort = effective.get(f"{prefix}reasoning_effort")
    if effort is None or provider in _SKIP_EFFORT_CHECK:
        return

    levels = ANTHROPIC_EFFORT_LEVELS if provider == "anthropic" else OPENAI_EFFORT_LEVELS
    if str(effort).lower() not in levels:
        raise ValueError(
            f"{label}reasoning_effort {effort!r} is not valid for provider {provider!r}; "
            f"expected one of {sorted(levels)}"
        )


def validate_ai_settings(effective: dict[str, Any]) -> None:
    """Validate a merged ai settings dict as it would read after a write."""
    validate_task_model("ai.", effective)


def validate_assistant_settings(effective: dict[str, Any]) -> None:
    """Validate a merged assistant settings dict as it would read after a write."""
    validate_task_model("assistant.", effective)
    validate_token_budget("assistant.max_tokens", effective.get("max_tokens"))


def validate_token_budget(name: str, value: Any) -> None:
    """The output budget of an orders or assistant task: 500 to 16000."""
    if value is not None and not (500 <= int(value) <= 16000):
        raise ValueError(f"{name} must be between 500 and 16000")


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
    if provider == "custom" and not effective.get("base_url"):
        raise ValueError("semantic.base_url is required when semantic.provider is 'custom'")
