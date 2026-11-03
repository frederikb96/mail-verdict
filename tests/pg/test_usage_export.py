"""
Token usage, recorded and exported against a real database: the gateway
writes a row per billed response, and the export pushes rows to a stub
Elasticsearch `_bulk` endpoint -- position, a resend's 409, a refused
document, the settle delay, no key, pruning.

Every test starts from an empty llm_usage table and no export position:
both are shared by the whole invocation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai.types.chat
import pytest
from sqlalchemy import delete, insert, select

from mail_verdict.config.loader import UsageExportConfig
from mail_verdict.core.retry import RetryConfig
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import CircuitBreakerState, LlmUsage, UsageExportState
from mail_verdict.pipeline.context import ModelGateway
from mail_verdict.usage.export import export_once, export_status, prune_once

pytestmark = pytest.mark.asyncio

_OLD = datetime.now(timezone.utc) - timedelta(minutes=10)


def _cfg(batch_size: int = 500) -> UsageExportConfig:
    return UsageExportConfig(
        elasticsearch_url="https://elastic.example.test", data_stream="logs-llm.usage-default",
        interval_seconds=60, batch_size=batch_size, request_timeout_seconds=5,
        retention_days=90,
    )


def _cred_repo(key: str | None = "a2V5") -> MagicMock:
    repo = MagicMock()
    repo.resolve_key = AsyncMock(return_value=key)
    return repo


async def _reset(db: DatabaseConnection) -> None:
    async with db.session() as session:
        await session.execute(delete(LlmUsage))
        await session.execute(delete(UsageExportState))


async def _seed(db: DatabaseConnection, count: int, created_at: datetime = _OLD) -> list[int]:
    async with db.session() as session:
        ids = (await session.execute(
            insert(LlmUsage).returning(LlmUsage.id),
            [
                {
                    "created_at": created_at, "purpose": "verdict", "provider": "infomaniak",
                    "model": "m", "input_tokens": 100 + i, "cache_read_tokens": 0,
                    "cache_write_tokens": 0, "output_tokens": 5,
                }
                for i in range(count)
            ],
        )).scalars().all()
    return sorted(ids)


class _Elastic:
    """A `_bulk` endpoint that answers per document: 201 for a new `_id`,
    409 for one it already holds, and the refusal `refuse` names."""

    def __init__(self, refuse: Callable[[str], bool] = lambda _id: False) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.refuse = refuse

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        lines = request.content.decode().splitlines()
        items = []
        for action, doc in zip(lines[::2], lines[1::2], strict=True):
            doc_id = json.loads(action)["create"]["_id"]
            if self.refuse(doc_id):
                items.append({"create": {"_id": doc_id, "status": 400, "error": {
                    "type": "document_parsing_exception", "reason": "strict mapping",
                }}})
            elif doc_id in self.docs:
                items.append({"create": {"_id": doc_id, "status": 409}})
            else:
                self.docs[doc_id] = json.loads(doc)
                items.append({"create": {"_id": doc_id, "status": 201}})
        return httpx.Response(200, json={"errors": False, "items": items})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


async def test_rows_are_sent_once_in_batches_with_their_own_ids(
    migrated_db: DatabaseConnection,
) -> None:
    await _reset(migrated_db)
    ids = await _seed(migrated_db, 5)
    elastic = _Elastic()
    cfg = _cfg(batch_size=2)

    sent = await export_once(migrated_db, _cred_repo(), cfg, transport=elastic.transport)
    again = await export_once(migrated_db, _cred_repo(), cfg, transport=elastic.transport)

    assert sent == 5
    assert again == 0
    assert set(elastic.docs) == {f"mail-verdict-{i}" for i in ids}
    assert all(r.url.path == "/logs-llm.usage-default/_bulk" for r in elastic.requests)
    assert elastic.requests[0].headers["authorization"] == "ApiKey a2V5"
    doc = elastic.docs[f"mail-verdict-{ids[0]}"]
    assert doc["llm"]["usage"] == {
        "app": "mail-verdict", "id": str(ids[0]), "purpose": "verdict",
        "provider": "infomaniak", "model": "m",
        "tokens": {"input": 100, "cache_read": 0, "cache_write": 0, "output": 5},
    }
    assert datetime.fromisoformat(doc["@timestamp"]).utcoffset() == timedelta(0)
    status = await export_status(migrated_db)
    assert status.pending == 0
    assert status.last_error is None
    assert status.last_success_at is not None


async def test_a_resend_answered_409_counts_as_delivered(
    migrated_db: DatabaseConnection,
) -> None:
    await _reset(migrated_db)
    ids = await _seed(migrated_db, 3)
    elastic = _Elastic()
    elastic.docs = {f"mail-verdict-{i}": {} for i in ids[:2]}

    sent = await export_once(migrated_db, _cred_repo(), _cfg(), transport=elastic.transport)

    assert sent == 3
    assert (await export_status(migrated_db)).pending == 0


async def test_a_refused_document_stops_the_batch_and_is_retried_next_pass(
    migrated_db: DatabaseConnection,
) -> None:
    await _reset(migrated_db)
    ids = await _seed(migrated_db, 4)
    blocked = {f"mail-verdict-{ids[1]}"}
    elastic = _Elastic(refuse=lambda doc_id: doc_id in blocked)

    sent = await export_once(migrated_db, _cred_repo(), _cfg(), transport=elastic.transport)

    assert sent == 1
    status = await export_status(migrated_db)
    assert status.pending == 3
    assert status.last_error is not None and "strict mapping" in status.last_error

    blocked.clear()
    await export_once(migrated_db, _cred_repo(), _cfg(), transport=elastic.transport)

    assert set(elastic.docs) == {f"mail-verdict-{i}" for i in ids}
    status = await export_status(migrated_db)
    assert status.pending == 0
    assert status.last_error is None


async def test_an_http_error_keeps_the_position_and_reports_it(
    migrated_db: DatabaseConnection,
) -> None:
    await _reset(migrated_db)
    await _seed(migrated_db, 2)
    transport = httpx.MockTransport(lambda _r: httpx.Response(401, text="missing authentication"))

    sent = await export_once(migrated_db, _cred_repo(), _cfg(), transport=transport)

    assert sent == 0
    status = await export_status(migrated_db)
    assert status.pending == 2
    assert status.last_error is not None and "401" in status.last_error


async def test_a_row_younger_than_the_settle_delay_waits(
    migrated_db: DatabaseConnection,
) -> None:
    await _reset(migrated_db)
    await _seed(migrated_db, 1)
    await _seed(migrated_db, 1, created_at=datetime.now(timezone.utc))
    elastic = _Elastic()

    sent = await export_once(migrated_db, _cred_repo(), _cfg(), transport=elastic.transport)

    assert sent == 1
    assert (await export_status(migrated_db)).pending == 1


async def test_nothing_is_sent_without_a_key(migrated_db: DatabaseConnection) -> None:
    await _reset(migrated_db)
    await _seed(migrated_db, 2)
    elastic = _Elastic()

    sent = await export_once(migrated_db, _cred_repo(key=None), _cfg(), transport=elastic.transport)

    assert sent == 0
    assert elastic.requests == []
    assert (await export_status(migrated_db)).pending == 2


async def test_prune_deletes_only_rows_past_retention(migrated_db: DatabaseConnection) -> None:
    await _reset(migrated_db)
    old = await _seed(migrated_db, 2, created_at=datetime.now(timezone.utc) - timedelta(days=91))
    kept = await _seed(migrated_db, 1)

    await prune_once(migrated_db, timedelta(days=90))

    async with migrated_db.session() as session:
        remaining = set((await session.execute(select(LlmUsage.id))).scalars())
    assert remaining == set(kept)
    assert not remaining & set(old)


async def test_the_gateway_records_every_billed_attempt_under_its_task(
    migrated_db: DatabaseConnection,
) -> None:
    await _reset(migrated_db)
    async with migrated_db.session() as session:
        await session.execute(
            delete(CircuitBreakerState).where(CircuitBreakerState.name == "custom:ai")
        )
    usage = {
        "prompt_tokens": 1200, "completion_tokens": 30, "total_tokens": 1230,
        "prompt_tokens_details": {"cached_tokens": 1000},
    }

    def _completion(content: str) -> Any:
        return openai.types.chat.ChatCompletion.model_validate({
            "id": "c", "object": "chat.completion", "created": 0, "model": "q",
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }],
            "usage": usage,
        })

    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[_completion("not json"), _completion('{"is_spam": false}')],
    )
    gateway = ModelGateway(
        migrated_db, MagicMock(),
        RetryConfig(max_retries=1, base_delay=0.0, max_delay=0.0, exp_base=2.0),
    )

    with patch(
        "mail_verdict.core.structured_llm.resolve_client", AsyncMock(return_value=client),
    ):
        await gateway.structured_call(
            provider="custom", category="ai", model="q", effort="none", max_tokens=100,
            schema_name="s", system_prompt="sys", user_prompt="u", schema={},
            base_url="https://api.infomaniak.com/2/ai/1/openai/v1",
        )

    async with migrated_db.session() as session:
        rows = (await session.execute(select(LlmUsage))).scalars().all()
    assert [
        (r.purpose, r.provider, r.model, r.input_tokens, r.cache_read_tokens, r.output_tokens)
        for r in rows
    ] == [("verdict", "infomaniak", "q", 1200, 1000, 30)] * 2
