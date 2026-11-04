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


_READ = _Settings(
    read_provider="custom", read_base_url="https://ai.example.test/v1",
    read_model="mistralai/Mistral-Small-4-119B-2603", read_reasoning_effort="none",
)


@pytest.mark.asyncio
async def test_an_image_is_read_on_the_read_tasks_own_model() -> None:
    gateway_call = AsyncMock(return_value=({"text": " EXAMPLE BAND "}, 900.0))
    with patch("mail_verdict.orders.model_calls.ModelGateway.structured_call", gateway_call):
        text = await model_calls.read_image(
            None, None, _READ, b"jpeg", filename="IMG_1.jpg",  # type: ignore[arg-type]
            ocr_confidence=45.0,
        )

    assert text == "EXAMPLE BAND"
    kwargs = gateway_call.await_args.kwargs
    assert (kwargs["category"], kwargs["model"]) == (
        "orders.read", "mistralai/Mistral-Small-4-119B-2603",
    )
    assert [(i.media_type, i.data) for i in kwargs["images"]] == [("image/jpeg", b"jpeg")]
    assert "IMG_1.jpg" in kwargs["user_prompt"]


@pytest.mark.asyncio
async def test_no_read_model_means_no_call() -> None:
    gateway_call = AsyncMock()
    with patch("mail_verdict.orders.model_calls.ModelGateway.structured_call", gateway_call):
        assert await model_calls.read_image(
            None, None, _Settings(), b"jpeg", filename="x.jpg",  # type: ignore[arg-type]
            ocr_confidence=45.0,
        ) is None
    gateway_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_read_keeps_the_ocr_text_instead_of_failing_the_job() -> None:
    from mail_verdict.pipeline.contracts import StageUnavailable

    gateway_call = AsyncMock(side_effect=StageUnavailable("custom circuit suspended"))
    with patch("mail_verdict.orders.model_calls.ModelGateway.structured_call", gateway_call):
        assert await model_calls.read_image(
            None, None, _READ, b"jpeg", filename="x.jpg",  # type: ignore[arg-type]
            ocr_confidence=45.0,
        ) is None


@pytest.mark.asyncio
async def test_an_image_ocr_read_well_is_not_read_again() -> None:
    gateway_call = AsyncMock()
    settings = _Settings(**{**_READ.get("orders"), "read_below_confidence": 70})
    with patch("mail_verdict.orders.model_calls.ModelGateway.structured_call", gateway_call):
        assert await model_calls.read_image(
            None, None, settings, b"jpeg", filename="scan.png",  # type: ignore[arg-type]
            ocr_confidence=91.0,
        ) is None
    gateway_call.assert_not_awaited()
