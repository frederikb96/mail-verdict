"""
0048_task_providers against the settings rows a released deployment holds,
not an empty database where nothing is copied: one provider on the ai
category serving the spam verdicts, the orders tasks and the rule
assistant, and one model/effort/budget set on orders for both of its calls.

After the upgrade every task reads what it read before: both orders tasks
carry the shared set and the provider and server address of ai, the
assistant starts from ai, and the old shared orders keys are gone. Values
that were never stored are not invented.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from tests.pg.test_migration_from_v1 import _POSTIMAP_STUBS, _alembic_config, _upgrade

_PREVIOUS = "0047_orders_wallet"
_REVISION = "0048_task_providers"

_BASE_URL = "https://api.infomaniak.com/2/ai/12345/openai/v1"
_AI = {
    "provider": "custom", "base_url": _BASE_URL, "model": "Qwen/Qwen3.8-27B-FP8",
    "reasoning_effort": "none", "max_tokens": 1024,
}


@pytest_asyncio.fixture()
async def db_at_previous(postgres_url: str) -> AsyncIterator[str]:
    name = f"taskproviders_{uuid.uuid4().hex[:12]}"
    admin_url = postgres_url.rsplit("/", 1)[0]
    admin = create_async_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    await admin.dispose()

    url = f"{admin_url}/{name}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        for statement in filter(None, (s.strip() for s in _POSTIMAP_STUBS.split(";"))):
            await conn.execute(text(statement))
    await engine.dispose()

    await _upgrade(url, _PREVIOUS)
    try:
        yield url
    finally:
        admin = create_async_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def _seed(url: str, **categories: dict[str, Any]) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            for category, data in categories.items():
                await conn.execute(
                    text("INSERT INTO settings (category, data) VALUES (:c, CAST(:d AS jsonb))"),
                    {"c": category, "d": json.dumps(data)},
                )
    finally:
        await engine.dispose()


async def _read(url: str, category: str) -> dict[str, Any] | None:
    """The stored JSONB, as an object -- a double-encoded string would come
    back as a str and fail every assertion on it."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT data FROM settings WHERE category = :c"), {"c": category},
                )
            ).first()
            if row is None:
                return None
            kind = await conn.scalar(
                text("SELECT jsonb_typeof(data) FROM settings WHERE category = :c"),
                {"c": category},
            )
            assert kind == "object"
            return dict(row.data)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_stored_ai_and_orders_blob_carries_over_to_both_tasks_and_the_assistant(
    db_at_previous: str,
) -> None:
    await _seed(
        db_at_previous,
        ai=_AI,
        semantic={"provider": "custom", "base_url": _BASE_URL, "model": "Qwen/Qwen3-Embedding-8B"},
        orders={
            "model": "Qwen/Qwen3.8-27B-FP8", "reasoning_effort": "none", "max_tokens": 6000,
            "language": "German", "auto_close_days": 45,
        },
    )

    await _upgrade(db_at_previous, _REVISION)

    orders = await _read(db_at_previous, "orders")
    assert orders is not None
    for task in ("decide", "write"):
        assert orders[f"{task}_provider"] == "custom"
        assert orders[f"{task}_base_url"] == _BASE_URL
        assert orders[f"{task}_model"] == "Qwen/Qwen3.8-27B-FP8"
        assert orders[f"{task}_reasoning_effort"] == "none"
        assert orders[f"{task}_max_tokens"] == 6000
    assert not {"model", "reasoning_effort", "max_tokens"} & set(orders)
    assert (orders["language"], orders["auto_close_days"]) == ("German", 45)

    assistant = await _read(db_at_previous, "assistant")
    assert assistant == {
        "provider": "custom", "base_url": _BASE_URL, "model": "Qwen/Qwen3.8-27B-FP8",
        "reasoning_effort": "none",
    }
    assert await _read(db_at_previous, "ai") == _AI
    semantic = await _read(db_at_previous, "semantic")
    assert semantic is not None and semantic["model"] == "Qwen/Qwen3-Embedding-8B"


@pytest.mark.asyncio
async def test_a_value_the_orders_blob_never_stored_reads_as_it_did_before(
    db_at_previous: str,
) -> None:
    """Only `model` was stored; the budget the old code read was its default,
    4000 -- not the new write task's larger default."""
    await _seed(db_at_previous, ai=_AI, orders={"model": "Qwen/Qwen3.8-27B-FP8"})

    await _upgrade(db_at_previous, _REVISION)

    orders = await _read(db_at_previous, "orders")
    assert orders is not None
    for task in ("decide", "write"):
        assert orders[f"{task}_max_tokens"] == 4000
        assert orders[f"{task}_reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_orders_without_a_stored_ai_blob_keep_the_code_defaults_for_provider(
    db_at_previous: str,
) -> None:
    await _seed(db_at_previous, orders={"model": "gpt-6-luna", "reasoning_effort": "low"})

    await _upgrade(db_at_previous, _REVISION)

    orders = await _read(db_at_previous, "orders")
    assert orders is not None
    assert orders["decide_model"] == orders["write_model"] == "gpt-6-luna"
    assert orders["decide_reasoning_effort"] == "low"
    assert "decide_provider" not in orders and "write_provider" not in orders
    assert await _read(db_at_previous, "assistant") is None


@pytest.mark.asyncio
async def test_a_stored_ai_blob_alone_still_moves_the_provider_to_the_orders_tasks(
    db_at_previous: str,
) -> None:
    """Orders had no model chosen yet, but whoever chooses one later must not
    find the tasks on a different provider than the one in use."""
    await _seed(db_at_previous, ai=_AI)

    await _upgrade(db_at_previous, _REVISION)

    orders = await _read(db_at_previous, "orders")
    assert orders == {
        "decide_provider": "custom", "decide_base_url": _BASE_URL,
        "write_provider": "custom", "write_base_url": _BASE_URL,
    }


@pytest.mark.asyncio
async def test_an_empty_settings_table_stays_empty(db_at_previous: str) -> None:
    await _upgrade(db_at_previous, _REVISION)

    assert await _read(db_at_previous, "orders") is None
    assert await _read(db_at_previous, "assistant") is None


@pytest.mark.asyncio
async def test_the_application_reads_the_migrated_blob_through_its_settings_service(
    db_at_previous: str,
) -> None:
    from mail_verdict.config.loader import DatabaseConfig
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.settings.service import SettingsService

    await _seed(db_at_previous, ai=_AI, orders={"model": "Qwen/Qwen3.8-27B-FP8"})
    await _upgrade(db_at_previous, _REVISION)

    db = DatabaseConnection(
        DatabaseConfig(
            url=db_at_previous, pool_size=2, max_overflow=0, reserved_for_requests=0,
        )
    )
    await db.init()
    try:
        service = SettingsService(db)
        await service.load()
        orders = service.get("orders")
        assert orders["write_model"] == "Qwen/Qwen3.8-27B-FP8"
        assert orders["write_provider"] == "custom"
        assert service.get("assistant")["model"] == "Qwen/Qwen3.8-27B-FP8"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_downgrade_folds_the_decide_task_back_into_the_shared_keys(
    db_at_previous: str,
) -> None:
    await _seed(db_at_previous, ai=_AI, orders={"model": "m", "max_tokens": 6000})
    await _upgrade(db_at_previous, _REVISION)

    await asyncio.to_thread(command.downgrade, _alembic_config(db_at_previous), _PREVIOUS)

    orders = await _read(db_at_previous, "orders")
    assert orders is not None
    assert (orders["model"], orders["max_tokens"]) == ("m", 6000)
    assert not any(k.startswith(("decide_", "write_")) for k in orders)
    assert await _read(db_at_previous, "assistant") is None


async def _insert_alert(url: str, kind: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO alerts (kind, deliver_at, dedupe_key) "
                    "VALUES (:k, now(), :d)"
                ),
                {"k": kind, "d": f"{kind}:{uuid.uuid4()}"},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_provider_unavailable_alert_kind_is_admitted_only_from_this_revision(
    db_at_previous: str,
) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError, match="ck_alerts_kind"):
        await _insert_alert(db_at_previous, "provider_unavailable")

    await _upgrade(db_at_previous, _REVISION)
    await _insert_alert(db_at_previous, "provider_unavailable")
    await _insert_alert(db_at_previous, "webhook_failed")

    await asyncio.to_thread(command.downgrade, _alembic_config(db_at_previous), _PREVIOUS)

    engine = create_async_engine(db_at_previous)
    try:
        async with engine.connect() as conn:
            kinds = (await conn.execute(text("SELECT kind FROM alerts"))).scalars().all()
    finally:
        await engine.dispose()
    assert kinds == ["webhook_failed"]
