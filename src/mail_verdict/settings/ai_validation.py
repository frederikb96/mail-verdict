"""
Validates the ai.provider / reasoning_effort combination at write time.

The two vendors spell reasoning effort differently and support different
levels, so an invalid pairing is rejected here rather than surfacing as a
classification failure on the next inbound message.
"""

from __future__ import annotations

from typing import Any

KNOWN_PROVIDERS = frozenset({"anthropic", "openai", "fake"})

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


def validate_ai_settings(effective: dict[str, Any]) -> None:
    """
    Validate a merged ai settings dict as it would read after a write.

    Args:
        effective: The ai settings dict with the incoming partial update
            already merged onto the existing values

    Raises:
        ValueError: If the provider is unknown, or reasoning_effort is not
            a level the selected provider supports
    """
    provider = str(effective.get("provider", "")).lower()
    if provider not in KNOWN_PROVIDERS:
        raise ValueError(
            f"ai.provider must be one of {sorted(KNOWN_PROVIDERS)}, got {provider!r}"
        )

    effort = effective.get("reasoning_effort")
    if effort is None or provider == "fake":
        return

    levels = ANTHROPIC_EFFORT_LEVELS if provider == "anthropic" else OPENAI_EFFORT_LEVELS
    if str(effort).lower() not in levels:
        raise ValueError(
            f"ai.reasoning_effort {effort!r} is not valid for provider {provider!r}; "
            f"expected one of {sorted(levels)}"
        )
