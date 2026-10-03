"""
The order controls against a real database: favorite / sealed / open-closed
changes and who owns open-closed, the list's favorites and text filters, a
sealed order being invisible to the order agent everywhere it could capture
a mail, the automatic close, a delete that leaves no order-linked row, and
the "none" decision creating nothing.

migrated_db is one database for the whole pg invocation, so every assertion
is scoped to the ids a test seeded itself.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update

from mail_verdict.api.orders import router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Order, OrderIdentifier, OrderJob, OrderMail
from mail_verdict.orders import repository
from mail_verdict.orders.auto_close import auto_close_once
from mail_verdict.orders.candidates import find_candidates
from mail_verdict.orders.intake import enqueue_thread_follow_up
from mail_verdict.orders.lookup import OrderLookup
from mail_verdict.orders.worker import _handle_mail_job
from mail_verdict.postimap.listener import PostimapEvent
from mail_verdict.settings.credentials import ProviderCredentialRepository
from tests.pg.test_orders_worker import (
    _enqueue_mail_job,
    _seed_account,
    _seed_folder,
    _seed_message,
    _settings_service,
)

_NOW = datetime.now(timezone.utc)


async def _written_order(
    db: DatabaseConnection, *, merchant: str = "Shop", subject: str = "Thing",
    status: str = "ordered", summary: str = "A thing.", is_open: bool = True,
    last_mail_age_days: float = 1.0, account_id: uuid.UUID | None = None,
    thread_id: uuid.UUID | None = None, identifiers: tuple[tuple[str, str], ...] = (),
    **columns: Any,
) -> uuid.UUID:
    """An order with one mail and a written text, the shape the list shows."""
    async with db.session() as session:
        order_id = await repository.create_order(session)
        await repository.attach_mail(
            session, order_id=order_id, account_id=account_id or uuid.uuid4(),
            msg_key=f"<{uuid.uuid4()}@example.com>", message_id=None, thread_id=thread_id,
            subject=subject, from_addr="shop@example.com",
            received_at=_NOW - timedelta(days=last_mail_age_days), attached_by="ai",
        )
        await repository.recompute_aggregates(session, order_id)
        await repository.write_order_text(
            session, order_id, merchant=merchant, subject=subject, status=status,
            is_open=is_open, icon="package", summary=summary, model="fake",
        )
        await repository.store_identifiers(session, order_id, list(identifiers))
        if columns:
            await session.execute(update(Order).where(Order.id == order_id).values(**columns))
    return order_id


async def _order(db: DatabaseConnection, order_id: uuid.UUID) -> Order:
    async with db.session() as session:
        return (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


class TestControlsAndOpenOwnership:
    @pytest.mark.asyncio
    async def test_a_persons_open_decision_is_recorded_and_sealing_leaves_it_alone(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        order_id = await _written_order(migrated_db)
        async with migrated_db.session() as session:
            assert await repository.update_controls(session, order_id, is_favorite=True)
            assert await repository.update_controls(session, order_id, is_sealed=True)
        order = await _order(migrated_db, order_id)
        assert (order.is_favorite, order.is_sealed) == (True, True)
        assert (order.is_open, order.open_set_by) == (True, "ai")

        async with migrated_db.session() as session:
            await repository.update_controls(session, order_id, is_open=False)
        order = await _order(migrated_db, order_id)
        assert (order.is_open, order.open_set_by) == (False, "user")

    @pytest.mark.asyncio
    async def test_a_missing_order_is_reported(self, migrated_db: DatabaseConnection) -> None:
        async with migrated_db.session() as session:
            assert not await repository.update_controls(session, uuid.uuid4(), is_favorite=True)

    @pytest.mark.asyncio
    async def test_a_write_call_changes_open_only_while_the_model_owns_it(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        model_owned = await _written_order(migrated_db, is_open=True)
        person_owned = await _written_order(migrated_db, is_open=True)
        async with migrated_db.session() as session:
            await repository.update_controls(session, person_owned, is_open=True)
            for order_id in (model_owned, person_owned):
                await repository.write_order_text(
                    session, order_id, merchant="Shop", subject="Thing", status="delivered",
                    is_open=False, icon="package", summary="Done.", model="fake",
                    expected_until=date(2026, 11, 12),
                )
        model_row = await _order(migrated_db, model_owned)
        person_row = await _order(migrated_db, person_owned)
        assert model_row.is_open is False
        assert person_row.is_open is True
        assert model_row.expected_until == person_row.expected_until == date(2026, 11, 12)

    @pytest.mark.asyncio
    async def test_a_new_mail_hands_open_closed_back_to_the_model(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        settings_service = await _settings_service(migrated_db)
        cred_repo = ProviderCredentialRepository(migrated_db, encryption_key="")
        thread_id = uuid.uuid4()
        async with migrated_db.session() as session:
            account_id = await _seed_account(session, orders_enabled=True)
            folder_id = await _seed_folder(session, account_id=account_id)
        order_id = await _written_order(
            migrated_db, account_id=account_id, thread_id=thread_id, is_open=False,
        )
        async with migrated_db.session() as session:
            await repository.update_controls(session, order_id, is_open=False)
            mail_id, key = await _seed_message(
                session, account_id=account_id, folder_id=folder_id,
                subject="Re: your parcel", thread_id=thread_id,
            )
            row = await _enqueue_mail_job(
                session, account_id=account_id, message_id=mail_id, msg_key=key,
            )
        assert (await _order(migrated_db, order_id)).open_set_by == "user"

        await _handle_mail_job(row, migrated_db, cred_repo, settings_service)

        order = await _order(migrated_db, order_id)
        assert order.mail_count == 2
        assert order.open_set_by == "ai"


class TestOrdersApi:
    def test_patch_toggles_and_the_favorites_list_returns_exactly_those(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        favorite = client.portal.call(_written_order, migrated_db)
        closed_favorite = client.portal.call(_written_order, migrated_db)
        plain = client.portal.call(_written_order, migrated_db)

        with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
            resp = client.patch(f"/orders/{favorite}", json={"is_favorite": True})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["is_favorite"] is True
            assert (body["is_open"], body["open_set_by"], body["is_sealed"]) == (True, "ai", False)
            assert body["expected_until"] is None

            resp = client.patch(
                f"/orders/{closed_favorite}", json={"is_favorite": True, "is_open": False},
            )
            assert resp.json()["open_set_by"] == "user"

            listed = client.get("/orders", params={"favorites": "true", "limit": 500}).json()
            ids = {item["id"] for item in listed["items"]}
            assert {str(favorite), str(closed_favorite)} <= ids
            assert str(plain) not in ids
            assert all(item["is_favorite"] for item in listed["items"])

            resp = client.patch(f"/orders/{favorite}", json={"is_favorite": False})
            assert resp.json()["is_favorite"] is False
            listed = client.get("/orders", params={"favorites": "true", "limit": 500}).json()
            assert str(favorite) not in {item["id"] for item in listed["items"]}

            sealed = client.patch(f"/orders/{plain}", json={"is_sealed": True}).json()
            assert sealed["is_sealed"] is True and sealed["is_open"] is True

    def test_patch_rejects_an_unknown_order_and_an_explicit_null(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        order_id = client.portal.call(_written_order, migrated_db)
        with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
            unknown = client.patch(f"/orders/{uuid.uuid4()}", json={"is_favorite": True})
            assert unknown.status_code == 404
            assert client.patch(f"/orders/{order_id}", json={"is_open": None}).status_code == 422

    def test_the_text_filter_matches_every_token_in_any_order_and_tolerates_a_typo(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        marker = uuid.uuid4().hex[:8]
        wanted = client.portal.call(
            functools.partial(
                _written_order, merchant="Zalando", subject=f"Running shoes {marker}",
                status="shipped", summary="Blue sneakers.",
            ),
            migrated_db,
        )
        other = client.portal.call(
            functools.partial(
                _written_order, merchant="Zalando", subject=f"Winter jacket {marker}",
                status="shipped",
            ),
            migrated_db,
        )

        def ids_for(query: str) -> set[str]:
            resp = client.get("/orders", params={"q": query, "limit": 500})
            assert resp.status_code == 200, resp.text
            return {item["id"] for item in resp.json()["items"]}

        with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
            assert ids_for(f"shoes zalando {marker}") == {str(wanted)}
            assert ids_for(f"{marker} running") == {str(wanted)}
            assert ids_for(f"zalandoo shoes {marker}") == {str(wanted)}
            assert ids_for(f"sneakers {marker}") == {str(wanted)}
            assert ids_for(f"zalando {marker}") == {str(wanted), str(other)}
            assert ids_for(f"shoes jacket {marker}") == set()


class TestSealedOrdersAreInvisibleToTheAgent:
    @pytest.mark.asyncio
    async def test_a_sealed_order_is_never_a_candidate_on_a_thread_or_a_number_match(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = uuid.uuid4()
        thread_id = uuid.uuid4()
        number = f"PQ-{uuid.uuid4().int % 10**8:08d}"
        order_id = await _written_order(
            migrated_db, account_id=account_id, thread_id=thread_id,
            identifiers=(("order_number", number),),
        )

        async def candidates() -> list[uuid.UUID]:
            async with migrated_db.session() as session:
                found = await find_candidates(
                    session, account_id=account_id, thread_id=thread_id,
                    subject=f"Order {number}", from_addr="shop@example.com",
                    haystack_raw=f"Order {number}",
                )
            return [c.order_id for c in found]

        assert order_id in await candidates()
        async with migrated_db.session() as session:
            await repository.update_controls(session, order_id, is_sealed=True)
        assert order_id not in await candidates()

    @pytest.mark.asyncio
    async def test_a_sealed_order_no_longer_makes_a_mail_known_to_the_register(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = uuid.uuid4()
        thread_id = uuid.uuid4()
        number = f"ZX-{uuid.uuid4().int % 10**8:08d}"
        order_id = await _written_order(
            migrated_db, account_id=account_id, thread_id=thread_id,
            identifiers=(("order_number", number),),
        )
        lookup = OrderLookup(migrated_db)
        assert await lookup.known(account_id=account_id, thread_id=thread_id, text="x") == "thread"
        assert (
            await lookup.known(account_id=account_id, thread_id=None, text=f"Order {number}")
            == "number"
        )
        async with migrated_db.session() as session:
            await repository.update_controls(session, order_id, is_sealed=True)
        assert await lookup.known(account_id=account_id, thread_id=thread_id, text="x") is None
        assert (
            await lookup.known(account_id=account_id, thread_id=None, text=f"Order {number}")
            is None
        )

    @pytest.mark.asyncio
    async def test_a_sent_reply_in_a_sealed_orders_thread_queues_nothing(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        thread_id = uuid.uuid4()
        async with migrated_db.session() as session:
            account_id = await _seed_account(session, orders_enabled=True)
            sent_folder = await _seed_folder(session, account_id=account_id, special_use="sent")
        order_id = await _written_order(
            migrated_db, account_id=account_id, thread_id=thread_id, is_sealed=True,
        )
        async with migrated_db.session() as session:
            reply_id, _ = await _seed_message(
                session, account_id=account_id, folder_id=sent_folder, subject="Re: thanks",
                thread_id=thread_id,
            )
        event = PostimapEvent(
            v=1, type="message", op="insert", id=str(reply_id), account_id=str(account_id),
            folder_id=str(sent_folder),
        )

        await enqueue_thread_follow_up(migrated_db, event)
        async with migrated_db.session() as session:
            jobs = (
                await session.execute(select(OrderJob).where(OrderJob.message_id == reply_id))
            ).scalars().all()
        assert jobs == []

        async with migrated_db.session() as session:
            await repository.update_controls(session, order_id, is_sealed=False)
        await enqueue_thread_follow_up(migrated_db, event)
        async with migrated_db.session() as session:
            jobs = (
                await session.execute(select(OrderJob).where(OrderJob.message_id == reply_id))
            ).scalars().all()
        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_a_sealed_order_does_not_stop_a_new_order_claiming_its_number(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        number = f"TRK{uuid.uuid4().int % 10**10:010d}"
        sealed = await _written_order(
            migrated_db, identifiers=(("tracking_number", number),), is_sealed=True,
        )
        fresh = await _written_order(migrated_db)
        async with migrated_db.session() as session:
            await repository.store_identifiers(session, fresh, [("tracking_number", number)])
            held = (
                await session.execute(
                    select(OrderIdentifier.order_id).where(
                        OrderIdentifier.order_id.in_([sealed, fresh]),
                        OrderIdentifier.value == number,
                    )
                )
            ).scalars().all()
        assert set(held) == {sealed, fresh}


class TestNoneDecision:
    @pytest.mark.asyncio
    async def test_a_none_decision_creates_no_order_membership_or_identifier(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        settings_service = await _settings_service(migrated_db)
        cred_repo = ProviderCredentialRepository(migrated_db, encryption_key="")
        async with migrated_db.session() as session:
            account_id = await _seed_account(session, orders_enabled=True)
            folder_id = await _seed_folder(session, account_id=account_id)
            mail_id, key = await _seed_message(
                session, account_id=account_id, folder_id=folder_id,
                subject="Our newsletter NK-48213",
            )
            row = await _enqueue_mail_job(
                session, account_id=account_id, message_id=mail_id, msg_key=key,
            )
            orders_before = (
                await session.execute(text("SELECT count(*) FROM orders"))
            ).scalar_one()
            identifiers_before = (
                await session.execute(text("SELECT count(*) FROM order_identifiers"))
            ).scalar_one()

        assert await _handle_mail_job(row, migrated_db, cred_repo, settings_service) is None

        async with migrated_db.session() as session:
            job = (
                await session.execute(select(OrderJob).where(OrderJob.id == row["id"]))
            ).scalar_one()
            memberships = (
                await session.execute(select(OrderMail).where(OrderMail.account_id == account_id))
            ).scalars().all()
            orders_after = (await session.execute(text("SELECT count(*) FROM orders"))).scalar_one()
            identifiers_after = (
                await session.execute(text("SELECT count(*) FROM order_identifiers"))
            ).scalar_one()
        assert job.outcome == "none" and job.order_id is None
        assert memberships == []
        assert (orders_after, identifiers_after) == (orders_before, identifiers_before)


class TestAutoClose:
    @pytest.mark.asyncio
    async def test_the_sweep_closes_only_what_nothing_is_expected_of(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        settings_service = await _settings_service(migrated_db)
        today = datetime.now(timezone.utc).date()

        undated_old = await _written_order(migrated_db, last_mail_age_days=31)
        undated_recent = await _written_order(migrated_db, last_mail_age_days=29)
        future_event = await _written_order(
            migrated_db, last_mail_age_days=90, expected_until=today + timedelta(days=20),
        )
        past_by_8 = await _written_order(
            migrated_db, last_mail_age_days=90, expected_until=today - timedelta(days=8),
        )
        past_by_6 = await _written_order(
            migrated_db, last_mail_age_days=90, expected_until=today - timedelta(days=6),
        )
        recent_mail_after_date = await _written_order(
            migrated_db, last_mail_age_days=3, expected_until=today - timedelta(days=20),
        )
        person_owned = await _written_order(
            migrated_db, last_mail_age_days=90, open_set_by="user",
        )
        stale_text = await _written_order(migrated_db, last_mail_age_days=90, text_stale=True)
        already_closed = await _written_order(migrated_db, last_mail_age_days=90, is_open=False)
        mine = [
            undated_old, undated_recent, future_event, past_by_8, past_by_6,
            recent_mail_after_date, person_owned, stale_text, already_closed,
        ]

        await auto_close_once(migrated_db, settings_service, None)

        rows = {order_id: await _order(migrated_db, order_id) for order_id in mine}
        closed = [o for o in mine if not rows[o].is_open and rows[o].open_set_by == "auto"]
        assert set(closed) == {undated_old, past_by_8}
        assert all(
            rows[o].is_open
            for o in (
                undated_recent, future_event, past_by_6, recent_mail_after_date, person_owned,
                stale_text,
            )
        )
        assert rows[person_owned].open_set_by == "user"
        assert rows[already_closed].open_set_by == "ai"

    @pytest.mark.asyncio
    async def test_a_zero_grace_closes_as_soon_as_the_date_has_passed(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        settings_service = await _settings_service(migrated_db)
        await settings_service.update("orders", {"auto_close_grace_days": 0})
        try:
            today = datetime.now(timezone.utc).date()
            passed = await _written_order(
                migrated_db, last_mail_age_days=5, expected_until=today - timedelta(days=1),
            )
            upcoming = await _written_order(
                migrated_db, last_mail_age_days=5, expected_until=today + timedelta(days=1),
            )
            await auto_close_once(migrated_db, settings_service, None)
            assert (await _order(migrated_db, passed)).is_open is False
            assert (await _order(migrated_db, upcoming)).is_open is True
        finally:
            await settings_service.update("orders", {"auto_close_grace_days": 7})

    @pytest.mark.asyncio
    async def test_zero_days_turns_the_close_off(self, migrated_db: DatabaseConnection) -> None:
        settings_service = await _settings_service(migrated_db)
        await settings_service.update("orders", {"auto_close_days": 0})
        try:
            undated = await _written_order(migrated_db, last_mail_age_days=400)
            dated = await _written_order(
                migrated_db, last_mail_age_days=400,
                expected_until=datetime.now(timezone.utc).date() - timedelta(days=100),
            )
            await auto_close_once(migrated_db, settings_service, None)
            assert (await _order(migrated_db, undated)).is_open is True
            assert (await _order(migrated_db, dated)).is_open is True
        finally:
            await settings_service.update("orders", {"auto_close_days": 30})

    @pytest.mark.asyncio
    async def test_a_reopened_order_stays_open_until_the_next_mail(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        settings_service = await _settings_service(migrated_db)
        order_id = await _written_order(migrated_db, last_mail_age_days=90)
        await auto_close_once(migrated_db, settings_service, None)
        assert (await _order(migrated_db, order_id)).open_set_by == "auto"

        async with migrated_db.session() as session:
            await repository.update_controls(session, order_id, is_open=True)
        await auto_close_once(migrated_db, settings_service, None)
        assert (await _order(migrated_db, order_id)).is_open is True


async def _delete_fixture(db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID, str]:
    """An order with a mail, an identifier, a pending write job and the
    mail job that bundled the mail -- plus that mail's own job row."""
    account_id = uuid.uuid4()
    msg_key = f"<{uuid.uuid4()}@example.com>"
    async with db.session() as session:
        order_id = await repository.create_order(session)
        await repository.attach_mail(
            session, order_id=order_id, account_id=account_id, msg_key=msg_key, message_id=None,
            thread_id=None, subject="s", from_addr="a@b.com", received_at=_NOW, attached_by="ai",
        )
        await repository.recompute_aggregates(session, order_id)
        await repository.store_identifiers(session, order_id, [("order_number", "NK-12345")])
        await repository.enqueue_write_job(session, order_id, priority=50)
        await repository.enqueue_mail_job(
            session, account_id=account_id, msg_key=msg_key, message_id=None, origin="live",
            priority=0, filter_reason="subject", next_attempt_at=_NOW,
        )
        await session.execute(
            update(OrderJob).where(OrderJob.msg_key == msg_key).values(order_id=order_id)
        )
    return order_id, account_id, msg_key


async def _references(db: DatabaseConnection, order_id: uuid.UUID, msg_key: str) -> dict[str, int]:
    async with db.session() as session:
        async def count(sql: str) -> int:
            return (await session.execute(text(sql), {"id": order_id, "k": msg_key})).scalar_one()

        return {
            "orders": await count("SELECT count(*) FROM orders WHERE id = :id"),
            "order_mails": await count("SELECT count(*) FROM order_mails WHERE order_id = :id"),
            "identifiers": await count(
                "SELECT count(*) FROM order_identifiers WHERE order_id = :id"
            ),
            "jobs_naming_it": await count("SELECT count(*) FROM order_jobs WHERE order_id = :id"),
            "mail_job_rows": await count(
                "SELECT count(*) FROM order_jobs WHERE kind = 'mail' AND msg_key = :k"
            ),
        }


class TestDeleteLeavesNothingBehind:
    @pytest.mark.asyncio
    async def test_delete_removes_every_row_naming_the_order_but_keeps_the_mail_job(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        order_id, _, msg_key = await _delete_fixture(migrated_db)
        before = await _references(migrated_db, order_id, msg_key)
        assert before == {
            "orders": 1, "order_mails": 1, "identifiers": 1, "jobs_naming_it": 2,
            "mail_job_rows": 1,
        }
        async with migrated_db.session() as session:
            assert await repository.delete_order(session, order_id)
        assert await _references(migrated_db, order_id, msg_key) == {
            "orders": 0, "order_mails": 0, "identifiers": 0, "jobs_naming_it": 0,
            "mail_job_rows": 1,
        }

    @pytest.mark.asyncio
    async def test_a_merge_source_leaves_nothing_behind_either(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        source, _, msg_key = await _delete_fixture(migrated_db)
        target = await _written_order(migrated_db)
        async with migrated_db.session() as session:
            await repository.merge_order(session, source_id=source, target_id=target)
        after = await _references(migrated_db, source, msg_key)
        assert (after["orders"], after["jobs_naming_it"], after["mail_job_rows"]) == (0, 0, 1)

    @pytest.mark.asyncio
    async def test_an_order_left_empty_by_a_detach_leaves_nothing_behind(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        order_id, _, msg_key = await _delete_fixture(migrated_db)
        async with migrated_db.session() as session:
            order_mail_id = (
                await session.execute(
                    select(OrderMail.id).where(OrderMail.order_id == order_id)
                )
            ).scalar_one()
            await repository.detach_mail(session, order_mail_id)
            assert await repository.recompute_aggregates(session, order_id) == 0
            assert await repository.delete_order_if_empty(session, order_id)
        after = await _references(migrated_db, order_id, msg_key)
        assert (after["orders"], after["jobs_naming_it"], after["mail_job_rows"]) == (0, 0, 1)
