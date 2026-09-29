"""
The list_orders/get_order MCP tools, against a real database, over
FastMCP's own in-memory Client rather than calling api/orders.py's
functions directly -- these tools are what an agent actually calls.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastmcp import Client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.mcp_tools import mcp
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.orders import repository

pytestmark = pytest.mark.asyncio

_TARGETS = ("mail_verdict.api.orders.get_db_connection",)


@pytest_asyncio.fixture()
async def mcp_client(migrated_db: DatabaseConnection) -> AsyncIterator[Client]:
    patchers = [patch(target, return_value=migrated_db) for target in _TARGETS]
    for p in patchers:
        p.start()
    try:
        async with Client(mcp) as client:
            yield client
    finally:
        for p in patchers:
            p.stop()


async def _seed_account(session: AsyncSession) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    return account_id


async def _seed_written_order(
    session: AsyncSession, *, account_id: uuid.UUID, subject: str,
) -> uuid.UUID:
    order_id = await repository.create_order(session)
    await repository.attach_mail(
        session, order_id=order_id, account_id=account_id, msg_key=f"<{uuid.uuid4()}@example.com>",
        message_id=None, thread_id=None, subject=subject, from_addr="shop@example.com",
        received_at=datetime.now(timezone.utc),
        attached_by="ai",
    )
    await repository.recompute_aggregates(session, order_id)
    await repository.write_order_text(
        session, order_id, merchant="Shop", subject=subject, status="shipped",
        is_open=True, icon="package", summary="On its way.", model="fake",
    )
    return order_id


class TestListOrdersTool:
    async def test_list_orders_returns_a_written_order(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            order_id = await _seed_written_order(
                session, account_id=account_id, subject="Your parcel has shipped",
            )

        result = await mcp_client.call_tool("list_orders", {})
        items = result.data
        assert any(item["id"] == str(order_id) for item in items)
        matched = next(item for item in items if item["id"] == str(order_id))
        assert matched["merchant"] == "Shop"
        assert matched["is_open"] is True
        assert str(account_id) in matched["account_ids"]

    async def test_list_orders_never_returns_an_order_with_no_write_yet(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            unwritten_order_id = await repository.create_order(session)
            await repository.attach_mail(
                session, order_id=unwritten_order_id, account_id=account_id,
                msg_key=f"<{uuid.uuid4()}@example.com>", message_id=None, thread_id=None,
                subject="Not written yet", from_addr="shop@example.com",
                received_at=datetime.now(timezone.utc),
                attached_by="ai",
            )
            await repository.recompute_aggregates(session, unwritten_order_id)

        result = await mcp_client.call_tool("list_orders", {})
        ids = {item["id"] for item in result.data}
        assert str(unwritten_order_id) not in ids


class TestGetOrderTool:
    async def test_get_order_returns_summary_and_mails(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            order_id = await _seed_written_order(
                session, account_id=account_id, subject="Your ticket is confirmed",
            )

        result = await mcp_client.call_tool("get_order", {"order_id": str(order_id)})
        detail = result.data
        assert "error" not in detail, detail
        assert detail["summary"] == "On its way."
        assert len(detail["mails"]) == 1
        assert detail["mails"][0]["subject"] == "Your ticket is confirmed"

    async def test_get_order_on_an_unknown_id_returns_an_error(
        self, mcp_client: Client,
    ) -> None:
        result = await mcp_client.call_tool("get_order", {"order_id": str(uuid.uuid4())})
        assert "error" in result.data
