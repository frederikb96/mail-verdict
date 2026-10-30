"""
0047_orders_wallet against the state the previous release reaches: orders
of every icon it produced with identifiers, mails and a pending write job
-- not an empty database, where the backfills never run. Identifiers must
survive as identifier-kind facts, kinds are seeded from the icon, the
activity pair starts equal to the mail pair, and the list index leads with
last_activity_at.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.pg.test_migration_from_v1 import _POSTIMAP_STUBS, _upgrade

_PREVIOUS = "0046_drop_unused_received_index"

_EXPECTED_KIND = {
    "train": "ticket", "plane": "ticket", "bus": "ticket", "bed": "ticket", "ticket": "ticket",
    "car": "ticket", "receipt": "other", "package": "order", "food": "order",
    "download": "order", "wrench": "order",
}


@pytest_asyncio.fixture()
async def db_at_previous(postgres_url: str) -> AsyncIterator[str]:
    name = f"orderwallet_{uuid.uuid4().hex[:12]}"
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


async def _rows(url: str, sql: str, **params: Any) -> list[Any]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return list((await conn.execute(text(sql), params)).all())
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_previous_release_orders_keep_their_identifiers_and_get_a_kind(
    db_at_previous: str,
) -> None:
    order_by_icon = {icon: uuid.uuid4() for icon in _EXPECTED_KIND}
    identifier_ids: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
    engine = create_async_engine(db_at_previous)
    try:
        async with engine.begin() as conn:
            for icon, order_id in order_by_icon.items():
                await conn.execute(
                    text(
                        "INSERT INTO orders (id, icon, text_stale, written_at, mail_count, "
                        "first_mail_at, last_mail_at) VALUES (:id, :icon, false, now(), 2, "
                        "'2026-03-01T10:00:00Z', '2026-03-05T10:00:00Z')"
                    ),
                    {"id": order_id, "icon": icon},
                )
            for order_id in order_by_icon.values():
                for kind, value, norm in (
                    ("tracking_number", "1Z999AA10123456784", "1Z999AA10123456784"),
                    ("booking_code", "AB-1234", "AB1234"),
                ):
                    identifier_id = uuid.uuid4()
                    identifier_ids[(order_id, kind)] = identifier_id
                    await conn.execute(
                        text(
                            "INSERT INTO order_identifiers (id, order_id, kind, value, "
                            "value_norm) VALUES (:id, :order_id, :kind, :value, :norm)"
                        ),
                        {
                            "id": identifier_id, "order_id": order_id, "kind": kind,
                            "value": value, "norm": norm,
                        },
                    )
            unwritten = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO orders (id, icon, text_stale, mail_count) "
                    "VALUES (:id, 'receipt', true, 0)"
                ),
                {"id": unwritten},
            )
    finally:
        await engine.dispose()

    await _upgrade(db_at_previous, "head")

    orders = {
        row.id: row
        for row in await _rows(
            db_at_previous,
            "SELECT id, icon, kind, kind_set_by, file_count, first_mail_at, last_mail_at, "
            "first_activity_at, last_activity_at, event_start FROM orders",
        )
    }
    for icon, order_id in order_by_icon.items():
        row = orders[order_id]
        assert row.kind == _EXPECTED_KIND[icon], icon
        assert row.kind_set_by == "ai"
        assert row.file_count == 0
        assert row.event_start is None
        assert (row.first_activity_at, row.last_activity_at) == (
            row.first_mail_at, row.last_mail_at,
        )
    assert orders[unwritten].last_activity_at is None

    facts = await _rows(
        db_at_previous,
        "SELECT id, order_id, kind, label, value, value_norm, source, position, carrier, "
        "document_id FROM order_facts",
    )
    assert len(facts) == 2 * len(order_by_icon)
    for fact in facts:
        assert identifier_ids[(fact.order_id, fact.kind)] == fact.id
        assert fact.source == "ai"
        assert fact.carrier is None and fact.document_id is None
        assert fact.label == {
            "tracking_number": "Tracking number", "booking_code": "Booking code",
        }[fact.kind]
        assert fact.value_norm == {
            "tracking_number": "1Z999AA10123456784", "booking_code": "AB1234",
        }[fact.kind]
    positions = {
        order_id: sorted(f.position for f in facts if f.order_id == order_id)
        for order_id in order_by_icon.values()
    }
    assert all(p == [0, 1] for p in positions.values())

    # The old table name is gone, and the partial unique index still refuses
    # the same number twice on one order.
    assert await _rows(db_at_previous, "SELECT to_regclass('order_identifiers') AS t") == [(None,)]
    engine = create_async_engine(db_at_previous)
    try:
        with pytest.raises(Exception, match="uq_order_facts_order_value_norm"):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO order_facts (order_id, kind, label, value, value_norm) "
                        "VALUES (:id, 'order_number', 'Order number', 'ab 1234', 'AB1234')"
                    ),
                    {"id": order_by_icon["train"]},
                )
    finally:
        await engine.dispose()

    index = await _rows(
        db_at_previous, "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_orders_list'",
    )
    assert "last_activity_at DESC" in index[0].indexdef
    assert "last_mail_at" not in index[0].indexdef


@pytest.mark.asyncio
async def test_upgrade_then_downgrade_restores_the_identifier_table(db_at_previous: str) -> None:
    from alembic import command
    from tests.pg.test_migration_from_v1 import _alembic_config

    order_id = uuid.uuid4()
    engine = create_async_engine(db_at_previous)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO orders (id, icon, mail_count) VALUES (:id, 'package', 1)"),
                {"id": order_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO order_identifiers (order_id, kind, value, value_norm) "
                    "VALUES (:id, 'order_number', 'NK-1', 'NK1')"
                ),
                {"id": order_id},
            )
    finally:
        await engine.dispose()

    await _upgrade(db_at_previous, "head")
    engine = create_async_engine(db_at_previous)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO order_facts (order_id, kind, label, value) "
                    "VALUES (:id, 'place', 'Station', 'Aachen Hbf')"
                ),
                {"id": order_id},
            )
    finally:
        await engine.dispose()

    import asyncio

    await asyncio.to_thread(command.downgrade, _alembic_config(db_at_previous), _PREVIOUS)

    rows = await _rows(db_at_previous, "SELECT kind, value, value_norm FROM order_identifiers")
    assert [tuple(r) for r in rows] == [("order_number", "NK-1", "NK1")]
