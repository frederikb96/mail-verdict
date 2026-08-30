"""Tests for validate_ai_settings: provider/reasoning_effort compatibility."""

from __future__ import annotations

import pytest

from mail_verdict.settings.ai_validation import validate_ai_settings


class TestValidCombinations:
    """Each provider accepts its own reasoning effort vocabulary."""

    def test_anthropic_with_valid_effort(self) -> None:
        validate_ai_settings({"provider": "anthropic", "reasoning_effort": "high"})

    def test_openai_with_valid_effort(self) -> None:
        validate_ai_settings({"provider": "openai", "reasoning_effort": "minimal"})

    def test_no_effort_specified_is_valid(self) -> None:
        validate_ai_settings({"provider": "openai"})

    def test_fake_provider_ignores_effort(self) -> None:
        """The fake provider never calls a model, so any effort value is harmless."""
        validate_ai_settings({"provider": "fake", "reasoning_effort": "not-a-real-level"})


class TestInvalidCombinations:
    """A mismatched provider/effort pair is rejected at write time."""

    def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(ValueError, match="ai.provider"):
            validate_ai_settings({"provider": "not-a-real-provider"})

    def test_openai_only_effort_rejected_for_anthropic(self) -> None:
        with pytest.raises(ValueError, match="reasoning_effort"):
            validate_ai_settings({"provider": "anthropic", "reasoning_effort": "minimal"})

    def test_garbage_effort_rejected(self) -> None:
        with pytest.raises(ValueError, match="reasoning_effort"):
            validate_ai_settings({"provider": "openai", "reasoning_effort": "ludicrous"})
