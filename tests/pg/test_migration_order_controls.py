"""
0043_order_controls against the state the previous release reaches: orders
of every shape it produced -- written and open, written and closed, never
written, one already owed a write -- not an empty database, where the
backfill loop never runs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.pg.test_migration_from_v1 import _POSTIMAP_STUBS, _upgrade

_PREVIOUS = "0042_webhooks"


@pytest_asyncio.fixture()
async def db_at_previous(postgres_url: str) -> AsyncIterator[str]:
    name = f"orderctl_{uuid.uuid4().hex[:12]}"
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


@pytest.mark.asyncio
async def test_open_written_orders_are_queued_for_a_rewrite_and_nothing_else_is(
    db_at_previous: str,
) -> None:
    open_written, open_with_job, closed_written, unwritten = (uuid.uuid4() for _ in range(4))
    engine = create_async_engine(db_at_previous)
    try:
        async with engine.begin() as conn:
            for order_id, is_open, written, stale in (
                (open_written, True, True, False),
                (open_with_job, True, True, True),
                (closed_written, False, True, False),
                (unwritten, True, False, True),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO orders (id, is_open, text_stale, written_at, mail_count, "
                        "last_mail_at) VALUES (:id, :open, :stale, "
                        "CASE WHEN :written THEN now() END, 1, now() - interval '40 days')"
                    ),
                    {"id": order_id, "open": is_open, "stale": stale, "written": written},
                )
            await conn.execute(
                text(
                    "INSERT INTO order_jobs (kind, order_id, origin, priority) "
                    "VALUES ('write', :id, 'live', 0)"
                ),
                {"id": open_with_job},
            )
    finally:
        await engine.dispose()

    await _upgrade(db_at_previous, "head")

    engine = create_async_engine(db_at_previous)
    try:
        async with engine.connect() as conn:
            orders = {
                row.id: row
                for row in (
                    await conn.execute(
                        text(
                            "SELECT id, text_stale, is_favorite, is_sealed, open_set_by, "
                            "expected_until FROM orders"
                        )
                    )
                ).all()
            }
            pending_writes = {
                row.order_id: row.n
                for row in (
                    await conn.execute(
                        text(
                            "SELECT order_id, count(*) AS n FROM order_jobs "
                            "WHERE kind = 'write' AND status = 'pending' GROUP BY order_id"
                        )
                    )
                ).all()
            }
    finally:
        await engine.dispose()

    for order in orders.values():
        assert (order.is_favorite, order.is_sealed, order.open_set_by) == (False, False, "ai")
        assert order.expected_until is None
    assert orders[open_written].text_stale is True
    assert orders[closed_written].text_stale is False
    assert pending_writes == {open_written: 1, open_with_job: 1}
