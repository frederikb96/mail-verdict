"""
orders/model_calls.py: the decide and write tasks each resolve their own
provider, model and budget from the orders settings, and each call goes to
the gateway under its own breaker category.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from mail_verdict.orders import model_calls
from mail_verdict.settings.defaults import SETTING_DEFAULTS, SettingCategory


class _Settings:
    def __init__(self, **orders: Any) -> None:
        self._categories = {
            "orders": {**SETTING_DEFAULTS[SettingCategory.ORDERS], **orders},
            "retry": dict(SETTING_DEFAULTS[SettingCategory.RETRY]),
        }

    def get(self, category: str) -> dict[str, Any]:
        return dict(self._categories[category])


_SPLIT = _Settings(
    decide_provider="custom", decide_base_url="https://ai.example.test/v1",
    decide_model="Qwen/Qwen3.8-27B-FP8", decide_reasoning_effort="none", decide_max_tokens=4000,
    write_provider="anthropic", write_model="claude-opus-5-5",
    write_reasoning_effort="medium", write_max_tokens=16000,
    call_timeout_seconds=120.0,
)


def test_each_task_reads_its_own_keys() -> None:
    decide = model_calls.TaskModel.from_settings(_SPLIT, "decide")  # type: ignore[arg-type]
    write = model_calls.TaskModel.from_settings(_SPLIT, "write")  # type: ignore[arg-type]

    assert (decide.provider, decide.base_url, decide.model) == (
        "custom", "https://ai.example.test/v1", "Qwen/Qwen3.8-27B-FP8",
    )
    assert (decide.effort, decide.max_tokens, decide.category) == ("none", 4000, "orders.decide")
    assert (write.provider, write.base_url, write.model) == ("anthropic", None, "claude-opus-5-5")
    assert (write.effort, write.max_tokens, write.category) == ("medium", 16000, "orders.write")
    assert decide.timeout_seconds == write.timeout_seconds == 120.0


def test_the_queue_reports_the_write_tasks_breaker() -> None:
    assert model_calls.write_circuit_name(_SPLIT) == "anthropic:orders.write"  # type: ignore[arg-type]


def test_fake_is_decided_per_task() -> None:
    settings = _Settings(decide_provider="fake")
    assert model_calls.is_fake(settings, "decide") is True  # type: ignore[arg-type]
    assert model_calls.is_fake(settings, "write") is False  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task", "provider", "category", "model", "base_url"),
    [
        ("decide", "custom", "orders.decide", "Qwen/Qwen3.8-27B-FP8", "https://ai.example.test/v1"),
        ("write", "anthropic", "orders.write", "claude-opus-5-5", None),
    ],
)
async def test_a_call_reaches_the_gateway_with_its_own_task_settings(
    task: str, provider: str, category: str, model: str, base_url: str | None,
) -> None:
    gateway_call = AsyncMock(return_value=({"ok": True}, 12.5))
    with patch("mail_verdict.orders.model_calls.ModelGateway.structured_call", gateway_call):
        data, model_name, latency = await model_calls.structured_call(
            None, None, _SPLIT, task,  # type: ignore[arg-type]
            schema_name="s", system_prompt="sys", user_prompt="usr", schema={}, validate=None,
        )

    assert (data, model_name, latency) == ({"ok": True}, model, 12.5)
    kwargs = gateway_call.await_args.kwargs
    assert (kwargs["provider"], kwargs["category"], kwargs["model"]) == (
        provider, category, model,
    )
    assert kwargs["base_url"] == base_url
    assert kwargs["timeout_seconds"] == 120.0
