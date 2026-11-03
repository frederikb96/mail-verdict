"""
Token counts of every provider's response, read through the call paths
that record them.

Each response is built from the provider SDK's own model, validated from a
wire-shaped body, so the field names are the SDK's and not this test's.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anthropic.types
import openai.types
import openai.types.chat
import openai.types.responses
import pytest

from mail_verdict.core.retry import RetryConfig
from mail_verdict.core.structured_llm import (
    call_anthropic_structured,
    call_chat_completions_structured,
    call_openai_structured,
)
from mail_verdict.embeddings.provider import OpenAIEmbeddingProvider
from mail_verdict.usage.tokens import TokenUsage, provider_label

pytestmark = pytest.mark.asyncio

_NO_RETRY = RetryConfig(max_retries=0, base_delay=0.0, max_delay=0.0, exp_base=2.0)
_ONE_RETRY = RetryConfig(max_retries=1, base_delay=0.0, max_delay=0.0, exp_base=2.0)


def _anthropic_message(text: str, usage: dict[str, Any]) -> anthropic.types.Message:
    return anthropic.types.Message.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "m",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None, "usage": usage,
    })


def _chat_completion(content: str, usage: dict[str, Any] | None) -> Any:
    body: dict[str, Any] = {
        "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": content},
        }],
    }
    if usage is not None:
        body["usage"] = usage
    return openai.types.chat.ChatCompletion.model_validate(body)


class _Collect:
    """An on_usage that keeps what it is handed."""

    def __init__(self) -> None:
        self.seen: list[TokenUsage] = []

    async def __call__(self, tokens: TokenUsage) -> None:
        self.seen.append(tokens)


async def test_anthropic_input_includes_cache_reads_and_writes() -> None:
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_anthropic_message('{"a": 1}', {
        "input_tokens": 100, "cache_read_input_tokens": 2000,
        "cache_creation_input_tokens": 300, "output_tokens": 40,
    }))
    seen = _Collect()

    await call_anthropic_structured(
        client, "m", None, 500, "sys", "user", {"type": "object"}, _NO_RETRY, on_usage=seen,
    )

    assert seen.seen == [TokenUsage(input=2400, cache_read=2000, cache_write=300, output=40)]


async def test_anthropic_without_cache_fields_counts_them_as_zero() -> None:
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_anthropic_message('{"a": 1}', {
        "input_tokens": 100, "output_tokens": 40,
    }))
    seen = _Collect()

    await call_anthropic_structured(
        client, "m", None, 500, "sys", "user", {"type": "object"}, _NO_RETRY, on_usage=seen,
    )

    assert seen.seen == [TokenUsage(input=100, cache_read=0, cache_write=0, output=40)]


async def test_chat_completions_cached_tokens_are_a_subset_of_input() -> None:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=_chat_completion('{"a": 1}', {
        "prompt_tokens": 1200, "completion_tokens": 90, "total_tokens": 1290,
        "prompt_tokens_details": {"cached_tokens": 1000, "cache_write_tokens": 150},
        "completion_tokens_details": {"reasoning_tokens": 60},
    }))
    seen = _Collect()

    await call_chat_completions_structured(
        client, "m", "low", 500, "s", "sys", "user", {"type": "object"}, _NO_RETRY,
        on_usage=seen,
    )

    assert seen.seen == [TokenUsage(input=1200, cache_read=1000, cache_write=150, output=90)]


async def test_every_billed_attempt_is_reported_including_a_malformed_one() -> None:
    """A response that fails to parse is retried, and was still paid for."""
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=[
        _chat_completion("not json", usage), _chat_completion('{"a": 1}', usage),
    ])
    seen = _Collect()

    await call_chat_completions_structured(
        client, "m", None, 500, "s", "sys", "user", {"type": "object"}, _ONE_RETRY,
        on_usage=seen,
    )

    assert len(seen.seen) == 2


async def test_a_response_without_usage_is_still_reported() -> None:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=_chat_completion('{"a": 1}', None))
    seen = _Collect()

    await call_chat_completions_structured(
        client, "m", None, 500, "s", "sys", "user", {"type": "object"}, _NO_RETRY,
        on_usage=seen,
    )

    assert seen.seen == [TokenUsage()]


async def test_responses_api_cached_tokens_are_a_subset_of_input() -> None:
    response = openai.types.responses.Response.model_validate({
        "id": "r1", "object": "response", "created_at": 0, "model": "m",
        "output": [{
            "type": "message", "id": "o1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": '{"a": 1}', "annotations": []}],
        }],
        "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
        "usage": {
            "input_tokens": 800,
            "input_tokens_details": {"cached_tokens": 500, "cache_write_tokens": 200},
            "output_tokens": 70, "output_tokens_details": {"reasoning_tokens": 50},
            "total_tokens": 870,
        },
    })
    client = MagicMock()
    client.responses.create = AsyncMock(return_value=response)
    seen = _Collect()

    await call_openai_structured(
        client, "m", None, 500, "s", "sys", "user", {"type": "object"}, _NO_RETRY,
        on_usage=seen,
    )

    assert seen.seen == [TokenUsage(input=800, cache_read=500, cache_write=200, output=70)]


async def test_embeddings_record_their_prompt_tokens() -> None:
    response = openai.types.CreateEmbeddingResponse.model_validate({
        "object": "list", "model": "e",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.0, 1.0]}],
        "usage": {"prompt_tokens": 321, "total_tokens": 321},
    })
    client = MagicMock()
    client.embeddings.create = AsyncMock(return_value=response)
    cred_repo = MagicMock()
    cred_repo.resolve_key = AsyncMock(return_value="sk-test")
    recorder = MagicMock()
    recorder.record = AsyncMock()
    provider = OpenAIEmbeddingProvider(
        cred_repo, provider="custom", base_url="https://api.infomaniak.com/1/ai/1/openai",
        usage=recorder,
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "mail_verdict.embeddings.provider.resolve_client", AsyncMock(return_value=client),
        )
        await provider.embed_batch(["text"], model="e")

    recorder.record.assert_awaited_once_with(
        provider="custom", base_url="https://api.infomaniak.com/1/ai/1/openai", model="e",
        tokens=TokenUsage(input=321),
    )


@pytest.mark.parametrize(
    ("provider", "base_url", "label"),
    [
        ("anthropic", None, "anthropic"),
        ("openai", None, "openai"),
        ("custom", "https://api.infomaniak.com/2/ai/123/openai/v1", "infomaniak"),
        ("custom", "https://llm.example.org/v1", "custom"),
        ("custom", "https://infomaniak.com.example.org/v1", "custom"),
    ],
)
async def test_provider_label_names_who_bills_the_call(
    provider: str, base_url: str | None, label: str,
) -> None:
    assert provider_label(provider, base_url) == label
