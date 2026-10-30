"""
Tests for validate_ai_settings/validate_semantic_settings: provider/
reasoning_effort compatibility, and the "custom" provider's base_url
requirement.
"""

from __future__ import annotations

import pytest

from mail_verdict.settings.ai_validation import (
    validate_ai_settings,
    validate_assistant_settings,
    validate_semantic_settings,
    validate_task_model,
)


class TestValidCombinations:
    """Each provider accepts its own reasoning effort vocabulary."""

    def test_anthropic_with_valid_effort(self) -> None:
        validate_ai_settings({"provider": "anthropic", "reasoning_effort": "high"})

    def test_openai_with_valid_effort(self) -> None:
        validate_ai_settings({"provider": "openai", "reasoning_effort": "minimal"})

    def test_openai_accepts_max(self) -> None:
        validate_ai_settings({"provider": "openai", "reasoning_effort": "max"})

    def test_no_effort_specified_is_valid(self) -> None:
        validate_ai_settings({"provider": "openai"})

    def test_fake_provider_ignores_effort(self) -> None:
        """The fake provider never calls a model, so any effort value is harmless."""
        validate_ai_settings({"provider": "fake", "reasoning_effort": "not-a-real-level"})

    def test_custom_provider_with_base_url_is_valid(self) -> None:
        validate_ai_settings({"provider": "custom", "base_url": "https://example.test/v1"})

    def test_custom_provider_ignores_effort(self) -> None:
        """No fixed vocabulary exists for an arbitrary compatible server."""
        validate_ai_settings({
            "provider": "custom", "base_url": "https://example.test/v1",
            "reasoning_effort": "whatever-this-server-happens-to-accept",
        })


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

    def test_custom_provider_without_base_url_rejected(self) -> None:
        with pytest.raises(ValueError, match="ai.base_url"):
            validate_ai_settings({"provider": "custom"})

    def test_custom_provider_with_empty_base_url_rejected(self) -> None:
        with pytest.raises(ValueError, match="ai.base_url"):
            validate_ai_settings({"provider": "custom", "base_url": ""})


class TestSemanticValidCombinations:
    def test_openai_provider_is_valid(self) -> None:
        validate_semantic_settings({"provider": "openai"})

    def test_fake_provider_is_valid(self) -> None:
        validate_semantic_settings({"provider": "fake"})

    def test_custom_provider_with_base_url_is_valid(self) -> None:
        validate_semantic_settings({"provider": "custom", "base_url": "https://example.test/v1"})


class TestSemanticInvalidCombinations:
    def test_anthropic_rejected(self) -> None:
        """No embedding model of Anthropic's own -- never a valid choice here."""
        with pytest.raises(ValueError, match="semantic.provider"):
            validate_semantic_settings({"provider": "anthropic"})

    def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(ValueError, match="semantic.provider"):
            validate_semantic_settings({"provider": "not-a-real-provider"})

    def test_custom_provider_without_base_url_rejected(self) -> None:
        with pytest.raises(ValueError, match="semantic.base_url"):
            validate_semantic_settings({"provider": "custom"})


class TestTaskModelWithPrefix:
    """validate_task_model checks one prefixed key group of a category, so
    two tasks in one category are validated independently."""

    def test_each_prefix_is_read_on_its_own(self) -> None:
        effective = {
            "decide_provider": "custom", "decide_base_url": "https://example.test/v1",
            "write_provider": "anthropic", "write_reasoning_effort": "medium",
        }
        validate_task_model("orders.decide_", effective, prefix="decide_")
        validate_task_model("orders.write_", effective, prefix="write_")

    def test_anthropic_rejects_none_effort(self) -> None:
        """The Anthropic API has no `none` effort; thinking cannot be switched off."""
        with pytest.raises(ValueError, match="orders.write_reasoning_effort"):
            validate_task_model(
                "orders.write_",
                {"write_provider": "anthropic", "write_reasoning_effort": "none"},
                prefix="write_",
            )

    def test_openai_accepts_none_effort(self) -> None:
        validate_task_model(
            "orders.write_", {"write_provider": "openai", "write_reasoning_effort": "none"},
            prefix="write_",
        )

    def test_the_error_names_the_task_that_failed(self) -> None:
        with pytest.raises(ValueError, match="orders.decide_provider"):
            validate_task_model("orders.decide_", {"decide_provider": "nope"}, prefix="decide_")


class TestAssistantValidation:
    def test_defaults_shape_is_valid(self) -> None:
        validate_assistant_settings(
            {"provider": "anthropic", "reasoning_effort": "medium", "max_tokens": 8000}
        )

    def test_custom_needs_its_own_base_url(self) -> None:
        with pytest.raises(ValueError, match="assistant.base_url"):
            validate_assistant_settings({"provider": "custom", "max_tokens": 8000})

    def test_token_budget_is_bounded(self) -> None:
        with pytest.raises(ValueError, match="assistant.max_tokens"):
            validate_assistant_settings({"provider": "openai", "max_tokens": 100})
