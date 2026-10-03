"""
The orders API's detach endpoint against a real database: the one status
code the design's own endpoint table specifies but no existing test
asserted on -- pg-layer tests call the repository function directly, the
browser tests only assert on DOM state.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from mail_verdict.api.orders import router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import OrderMail
from mail_verdict.orders import repository

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """A single persistent TestClient portal for the whole test -- a new
    `with TestClient(...)` per call would each open its own event loop in
    its own thread, and the shared migrated_db's asyncpg connections
    would then bounce between them and fail with 'attached to a
    different loop' the moment a second call touches the database."""
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


async def _attach(
    migrated_db: DatabaseConnection, *, order_id: uuid.UUID, subject: str,
) -> uuid.UUID:
    """Attach one mail to an order and return its order_mails.id (the
    detach route's `mail_key`) -- no accounts/messages rows needed, since
    order_mails.account_id carries no foreign key onto any PostIMAP-owned
    table and a None message_id resolves to "gone" without a lookup."""
    async with migrated_db.session() as session:
        msg_key = f"<{uuid.uuid4()}@example.com>"
        await repository.attach_mail(
            session, order_id=order_id, account_id=uuid.uuid4(), msg_key=msg_key,
            message_id=None, thread_id=None, subject=subject, from_addr="shop@example.com",
            received_at=_NOW, attached_by="ai",
        )
        row_id = (
            await session.execute(
                select(OrderMail.id).where(
                    OrderMail.order_id == order_id, OrderMail.msg_key == msg_key,
                )
            )
        ).scalar_one()
    return row_id


async def _seed_order(migrated_db: DatabaseConnection) -> uuid.UUID:
    async with migrated_db.session() as session:
        return await repository.create_order(session)


class TestDetachAnswersTheDocumentedStatus:
    def test_emptying_an_order_answers_204_with_no_body(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        order_id = client.portal.call(_seed_order, migrated_db)
        mail_key = client.portal.call(
            functools.partial(_attach, order_id=order_id, subject="Order NK-1 confirmed"),
            migrated_db,
        )

        with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
            resp = client.post(f"/orders/{order_id}/mails/{mail_key}/detach", json={})

        assert resp.status_code == 204, resp.text
        assert resp.content == b""

    def test_emptying_an_order_by_moving_its_last_mail_also_answers_204(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        source_id = client.portal.call(_seed_order, migrated_db)
        target_id = client.portal.call(_seed_order, migrated_db)
        mail_key = client.portal.call(
            functools.partial(_attach, order_id=source_id, subject="Order NK-2 confirmed"),
            migrated_db,
        )

        with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
            resp = client.post(
                f"/orders/{source_id}/mails/{mail_key}/detach",
                json={"move_to": str(target_id)},
            )

        assert resp.status_code == 204, resp.text
        assert resp.content == b""

    def test_a_detach_that_leaves_mail_behind_answers_200_with_the_order(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        order_id = client.portal.call(_seed_order, migrated_db)
        client.portal.call(
            functools.partial(_attach, order_id=order_id, subject="Order NK-3 confirmed"),
            migrated_db,
        )
        mail_key_2 = client.portal.call(
            functools.partial(_attach, order_id=order_id, subject="NK-3 has shipped"),
            migrated_db,
        )

        with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
            resp = client.post(f"/orders/{order_id}/mails/{mail_key_2}/detach", json={})

        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == str(order_id)
