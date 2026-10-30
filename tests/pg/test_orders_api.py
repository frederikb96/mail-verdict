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
from datetime import date, datetime, timedelta, timezone
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


async def _seed_written(
    migrated_db: DatabaseConnection, *, merchant: str, kind: str, received_at: datetime,
    event_start: date | None = None, is_open: bool = True,
) -> uuid.UUID:
    """A written order with one mail, as the write call would leave it."""
    async with migrated_db.session() as session:
        order_id = await repository.create_order(session)
        await repository.attach_mail(
            session, order_id=order_id, account_id=uuid.uuid4(),
            msg_key=f"<{uuid.uuid4()}@example.com>", message_id=None, thread_id=None,
            subject=merchant, from_addr="shop@example.com", received_at=received_at,
            attached_by="ai",
        )
        await repository.recompute_aggregates(session, order_id)
        await repository.write_order_text(
            session, order_id, merchant=merchant, subject="Item", status="ordered",
            is_open=is_open, icon="package", summary="s", model="fake", kind=kind,
            event_start=event_start,
        )
    return order_id


def _list(client: TestClient, migrated_db: DatabaseConnection, **params: object) -> dict:  # type: ignore[type-arg]
    with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
        resp = client.get("/orders", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


class TestListByKindAndSort:
    def test_kind_filters_and_the_default_sort_is_last_activity(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        token = f"Kindshop{uuid.uuid4().hex[:8]}"
        ticket, order, other = (
            client.portal.call(
                functools.partial(
                    _seed_written, merchant=f"{token} {name}", kind=name,
                    received_at=_NOW - timedelta(days=days),
                ),
                migrated_db,
            )
            for name, days in (("ticket", 2), ("order", 1), ("other", 3))
        )

        everything = _list(client, migrated_db, q=token)
        assert [i["id"] for i in everything["items"]] == [str(order), str(ticket), str(other)]
        assert {i["id"]: i["kind"] for i in everything["items"]} == {
            str(ticket): "ticket", str(order): "order", str(other): "other",
        }
        assert all(i["kind_set_by"] == "ai" for i in everything["items"])
        for wanted in (ticket, order, other):
            kind = next(i["kind"] for i in everything["items"] if i["id"] == str(wanted))
            only = _list(client, migrated_db, q=token, kind=kind)
            assert [i["id"] for i in only["items"]] == [str(wanted)]

        with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
            assert client.get("/orders", params={"kind": "bogus"}).status_code == 422
            assert client.get("/orders", params={"sort": "bogus"}).status_code == 422

    def test_sort_event_orders_by_the_timeline_date_and_pages_on_it(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        token = f"Eventshop{uuid.uuid4().hex[:8]}"
        spec = (
            # (name, received day offset, event_start): the timeline date is the
            # event start, else the UTC date of the first mail.
            ("far", 10, date(2026, 9, 1)),
            ("soon", 20, date(2026, 6, 10)),
            ("noevent", 3, None),  # first activity 2026-05-29 -> sorts last
            ("sameday", 30, date(2026, 6, 10)),
        )
        ids = {
            name: client.portal.call(
                functools.partial(
                    _seed_written, merchant=f"{token} {name}", kind="ticket",
                    received_at=_NOW - timedelta(days=days), event_start=event,
                ),
                migrated_db,
            )
            for name, days, event in spec
        }

        first = _list(client, migrated_db, q=token, sort="event", limit=2)
        ordered_same_day = sorted([ids["soon"], ids["sameday"]], key=str, reverse=True)
        expected = [ids["far"], *ordered_same_day, ids["noevent"]]
        assert [i["id"] for i in first["items"]] == [str(e) for e in expected[:2]]
        assert first["has_more"] is True

        second = _list(
            client, migrated_db, q=token, sort="event", limit=2, before=first["next_cursor"],
        )
        assert [i["id"] for i in second["items"]] == [str(e) for e in expected[2:]]
        assert second["has_more"] is False

        dates = {
            i["id"]: i["timeline_date"]
            for i in first["items"] + second["items"]
        }
        assert dates[str(ids["far"])] == "2026-09-01"
        assert dates[str(ids["noevent"])] == "2026-05-29"

    def test_an_event_date_beats_the_first_activity_in_timeline_date_for_the_detail_too(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        order_id = client.portal.call(
            functools.partial(
                _seed_written, merchant="Detaildate", kind="ticket", received_at=_NOW,
                event_start=date(2026, 12, 24),
            ),
            migrated_db,
        )
        with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
            detail = client.get(f"/orders/{order_id}").json()
        assert detail["timeline_date"] == "2026-12-24"
        assert detail["event_start"] == "2026-12-24"
        assert detail["event_end"] is None


class TestPatchKind:
    def test_a_persons_kind_sticks_through_a_rewrite(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        order_id = client.portal.call(
            functools.partial(
                _seed_written, merchant="Kindfix", kind="order", received_at=_NOW,
            ),
            migrated_db,
        )
        with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
            resp = client.patch(f"/orders/{order_id}", json={"kind": "other"})
            assert resp.status_code == 200, resp.text
            assert (resp.json()["kind"], resp.json()["kind_set_by"]) == ("other", "user")

            assert client.patch(f"/orders/{order_id}", json={"kind": "bogus"}).status_code == 422
            assert client.patch(f"/orders/{order_id}", json={"kind": None}).status_code == 422

            async def rewrite() -> None:
                async with migrated_db.session() as session:
                    await repository.write_order_text(
                        session, order_id, merchant="Kindfix", subject="Item", status="shipped",
                        is_open=True, icon="package", summary="s2", model="fake", kind="ticket",
                        event_start=date(2026, 7, 1),
                    )

            client.portal.call(rewrite)
            detail = client.get(f"/orders/{order_id}").json()

        assert detail["kind"] == "other" and detail["kind_set_by"] == "user"
        assert detail["status"] == "shipped"
