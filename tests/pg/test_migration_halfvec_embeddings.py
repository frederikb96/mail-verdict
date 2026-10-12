"""
0045 turns message_embeddings.embedding into halfvec(1536), rebuilds the HNSW
index on it, and copies each message's received_at onto its embedding row.
Migrating an empty database cannot show any of that, so these tests migrate
a database holding rows the previous release produced: a live message, a
glaciered one, a row whose join hint no longer resolves, and one not yet
embedded.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from tests.pg.test_migration_from_v1 import _POSTIMAP_STUBS, _alembic_config

_PREVIOUS = "0044_drop_default_strictness"
_LIVE_AT = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
_GLACIER_AT = datetime(2025, 7, 2, 8, 30, tzinfo=timezone.utc)


def _vector(first: float, second: float) -> str:
    return json.dumps([first, second] + [0.0] * 1534)


async def _upgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.upgrade, _alembic_config(url), revision)


@pytest_asyncio.fixture()
async def db_at_0044(postgres_url: str) -> AsyncIterator[str]:
    """A throwaway database migrated to 0044 (the last revision before this
    migration), with the PostIMAP tables earlier revisions read stubbed.
    Dropped afterwards."""
    name = f"halfvec_{uuid.uuid4().hex[:12]}"
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
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n"
                ),
                {"n": name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        await admin.dispose()


async def _downgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.downgrade, _alembic_config(url), revision)


async def _seed(url: str) -> dict[str, uuid.UUID]:
    """Rows as the previous release left them; returns their embedding ids."""
    account_id, live_id, glacier_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ids = {name: uuid.uuid4() for name in ("live", "glacier", "dead", "pending")}
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO messages (id, account_id, received_at) VALUES (:id, :a, :at)"),
            {"id": live_id, "a": account_id, "at": _LIVE_AT},
        )
        await conn.execute(
            text(
                "INSERT INTO glacier_messages (id, account_id, folder_id, thread_id, "
                "message_id, subject, from_addr, body_text, raw_source, size_bytes, "
                "received_at, msg_key, state, visible_at, glaciered_at) VALUES "
                "(:id, :a, :f, :id, '<g@example.com>', 's', 'x@example.com', 'b', 'raw', 3, "
                ":at, 'glacier-key', 'glaciered', now(), now())"
            ),
            {"id": glacier_id, "a": account_id, "f": uuid.uuid4(), "at": _GLACIER_AT},
        )
        rows = [
            ("live", "live-key", live_id, "done", _vector(1.0, 0.5)),
            ("glacier", "glacier-key", None, "done", _vector(0.25, 1.0)),
            ("dead", "dead-key", uuid.uuid4(), "done", _vector(0.5, 0.5)),
            ("pending", "pending-key", live_id, "pending", None),
        ]
        for name, key, hint, status, vector in rows:
            await conn.execute(
                text(
                    "INSERT INTO message_embeddings "
                    "(id, account_id, msg_key, message_id, model, status, embedding) "
                    "VALUES (:id, :a, :k, :h, 'm', :s, CAST(:v AS vector))"
                ),
                {"id": ids[name], "a": account_id, "k": key, "h": hint, "s": status, "v": vector},
            )
    await engine.dispose()
    return ids


async def _query(url: str, sql: str, **params: object) -> list[tuple[object, ...]]:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        rows = [tuple(row) for row in (await conn.execute(text(sql), params)).all()]
    await engine.dispose()
    return rows


_COLUMN_TYPE = (
    "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
    "WHERE attrelid = 'message_embeddings'::regclass AND attname = 'embedding'"
)
_INDEXES = (
    "SELECT indexname, indexdef FROM pg_indexes "
    "WHERE tablename IN ('message_embeddings', 'glacier_messages')"
)


@pytest.mark.asyncio
async def test_the_column_becomes_halfvec_and_keeps_its_values(db_at_0044: str) -> None:
    ids = await _seed(db_at_0044)

    await _upgrade(db_at_0044, "0045_halfvec_embeddings")

    assert (await _query(db_at_0044, _COLUMN_TYPE)) == [("halfvec(1536)",)]
    (similarity,) = (await _query(
        db_at_0044,
        "SELECT 1 - (embedding <=> CAST(:q AS halfvec)) FROM message_embeddings WHERE id = :id",
        q=_vector(1.0, 0.5), id=ids["live"],
    ))[0]
    assert float(similarity) > 0.9999  # type: ignore[arg-type]
    assert (await _query(
        db_at_0044, "SELECT embedding IS NULL FROM message_embeddings WHERE id = :id",
        id=ids["pending"],
    )) == [(True,)]


@pytest.mark.asyncio
async def test_received_at_is_copied_from_the_live_and_the_glacier_row(db_at_0044: str) -> None:
    ids = await _seed(db_at_0044)

    await _upgrade(db_at_0044, "0045_halfvec_embeddings")

    got = dict(await _query(db_at_0044, "SELECT id, received_at FROM message_embeddings"))  # type: ignore[arg-type]
    assert got[ids["live"]] == _LIVE_AT
    assert got[ids["pending"]] == _LIVE_AT
    assert got[ids["glacier"]] == _GLACIER_AT
    # Its message cannot be found, so nothing to copy.
    assert got[ids["dead"]] is None
    mismatched = await _query(
        db_at_0044,
        "SELECT count(*) FROM message_embeddings me JOIN messages m "
        "ON m.id = me.message_id AND m.account_id = me.account_id "
        "WHERE me.received_at IS DISTINCT FROM m.received_at",
    )
    assert mismatched == [(0,)]


@pytest.mark.asyncio
async def test_the_hnsw_index_is_rebuilt_on_halfvec_and_the_btrees_exist(db_at_0044: str) -> None:
    await _seed(db_at_0044)

    await _upgrade(db_at_0044, "0045_halfvec_embeddings")

    indexes = dict(await _query(db_at_0044, _INDEXES))  # type: ignore[arg-type]
    hnsw = str(indexes["ix_message_embeddings_hnsw"])
    assert "halfvec_cosine_ops" in hnsw and "status = 'done'" in hnsw
    assert "ix_message_embeddings_message_id" in indexes
    assert "(account_id, received_at)" in str(indexes["ix_message_embeddings_account_received"])
    assert "(account_id, received_at)" in str(indexes["idx_glacier_messages_account_received"])
    valid = await _query(
        db_at_0044,
        "SELECT count(*) FROM pg_index WHERE NOT indisvalid "
        "AND indrelid IN ('message_embeddings'::regclass, 'glacier_messages'::regclass)",
    )
    assert valid == [(0,)]


@pytest.mark.asyncio
async def test_downgrade_restores_the_vector_column_and_its_index(db_at_0044: str) -> None:
    ids = await _seed(db_at_0044)
    await _upgrade(db_at_0044, "0045_halfvec_embeddings")

    await _downgrade(db_at_0044, _PREVIOUS)

    assert (await _query(db_at_0044, _COLUMN_TYPE)) == [("vector(1536)",)]
    indexes = dict(await _query(db_at_0044, _INDEXES))  # type: ignore[arg-type]
    assert "vector_cosine_ops" in str(indexes["ix_message_embeddings_hnsw"])
    assert "ix_message_embeddings_message_id" not in indexes
    assert "idx_glacier_messages_account_received" not in indexes
    columns = await _query(
        db_at_0044,
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'message_embeddings' AND column_name = 'received_at'",
    )
    assert columns == [(0,)]
    assert (await _query(
        db_at_0044, "SELECT count(*) FROM message_embeddings WHERE id = :id", id=ids["live"],
    )) == [(1,)]

    # And forward again over the downgraded rows.
    await _upgrade(db_at_0044, "0045_halfvec_embeddings")
    assert (await _query(db_at_0044, _COLUMN_TYPE)) == [("halfvec(1536)",)]
