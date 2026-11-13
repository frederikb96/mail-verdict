"""
Orders belong to one user: the worker stamps the owner of the mail's account,
retrieval, number lookup and number ownership see only that user's orders,
the sweeps judge each order by its owner's settings, the routes and files act
for the caller alone, and an account given to another user takes its orders
along (or out of the previous owner's).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mail_verdict.api.orders import router
from mail_verdict.auth.principal import OWNER_USER_ID
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Order, OrderFact, OrderFile, OrderJob, OrderMail
from mail_verdict.orders import repository
from mail_verdict.orders.auto_close import auto_close_once
from mail_verdict.orders.candidates import find_candidates
from mail_verdict.orders.catch_up import run_catch_up
from mail_verdict.orders.lookup import OrderLookup
from mail_verdict.orders.ownership import reassign_account
from mail_verdict.orders.worker import _handle_file_job, _handle_mail_job, _handle_write_job
from mail_verdict.settings.credentials import ProviderCredentialRepository
from tests.helpers.auth import act_as, user_scope
from tests.helpers.orders_pg import ModelStub, ticket_pdf
from tests.helpers.pipeline_pg import insert_user
from tests.pg.test_orders_documents import _stub_tasks
from tests.pg.test_orders_worker import (
    _enqueue_mail_job,
    _seed_account,
    _seed_folder,
    _seed_message,
    _settings_service,
)

_NOW = datetime.now(timezone.utc)


async def _written_order(
    db: DatabaseConnection, user_id: uuid.UUID, account_id: uuid.UUID, *,
    number: str | None = None, last_mail_age_days: float = 1.0,
    extra_account_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """An order of `user_id` with a mail from `account_id` (and one from
    `extra_account_id`), a written text and optionally an order number."""
    async with db.session() as session:
        order_id = await repository.create_order(session, user_id=user_id)
        for account in (account_id, extra_account_id):
            if account is None:
                continue
            await repository.attach_mail(
                session, order_id=order_id, account_id=account,
                msg_key=f"<{uuid.uuid4()}@example.com>", message_id=None, thread_id=None,
                subject="Your order", from_addr="shop@example.com",
                received_at=_NOW - timedelta(days=last_mail_age_days), attached_by="ai",
            )
        await repository.recompute_aggregates(session, order_id)
        await repository.write_order_text(
            session, order_id, merchant="Shop", subject="Your order", status="ordered",
            is_open=True, icon="package", summary="A thing.", model="fake",
        )
        if number is not None:
            await repository.store_identifier_facts(
                session, order_id, [("order_number", number)],
            )
    return order_id


async def _two_users(db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Users A and B, each owning one account with orders switched on."""
    user_a, user_b = await insert_user(db), await insert_user(db)
    async with db.session() as session:
        account_a = await _seed_account(session, orders_enabled=True, owner_id=user_a)
        account_b = await _seed_account(session, orders_enabled=True, owner_id=user_b)
    return user_a, user_b, account_a, account_b


@pytest.mark.asyncio
class TestTwoUsersNeverShareAnOrder:
    async def test_mail_about_one_order_number_makes_one_order_per_user(
        self, migrated_db: DatabaseConnection, number: str,
    ) -> None:
        settings_service = await _settings_service(migrated_db)
        cred_repo = ProviderCredentialRepository(migrated_db, encryption_key="")
        user_a, user_b, account_a, account_b = await _two_users(migrated_db)
        async with migrated_db.session() as session:
            folder_a = await _seed_folder(session, account_id=account_a)
            folder_b = await _seed_folder(session, account_id=account_b)
            mail_a, key_a = await _seed_message(
                session, account_id=account_a, folder_id=folder_a,
                subject=f"Your order {number} is confirmed",
                body_text=f"Order number: {number}. Thanks.",
            )
            mail_b, key_b = await _seed_message(
                session, account_id=account_b, folder_id=folder_b,
                subject=f"Order {number} has shipped", received_at=_NOW + timedelta(minutes=5),
                body_text=f"Order number: {number}. On its way.",
            )
            row_a = await _enqueue_mail_job(
                session, account_id=account_a, message_id=mail_a, msg_key=key_a,
            )
            row_b = await _enqueue_mail_job(
                session, account_id=account_b, message_id=mail_b, msg_key=key_b,
            )

        await _handle_mail_job(row_a, migrated_db, cred_repo, settings_service)
        await _handle_mail_job(row_b, migrated_db, cred_repo, settings_service)

        async with migrated_db.session() as session:
            rows = (
                await session.execute(
                    select(OrderMail.account_id, Order.id, Order.user_id, Order.mail_count)
                    .join(Order, Order.id == OrderMail.order_id)
                    .where(OrderMail.account_id.in_((account_a, account_b)))
                )
            ).all()
            holders = (
                await session.execute(
                    select(OrderFact.order_id).where(
                        OrderFact.value_norm == number.upper(),
                    )
                )
            ).scalars().all()
        by_account = {row.account_id: row for row in rows}
        assert by_account[account_a].user_id == user_a
        assert by_account[account_b].user_id == user_b
        assert by_account[account_a].id != by_account[account_b].id
        assert {row.mail_count for row in rows} == {1}
        # Both users' orders hold the number: neither was refused it for
        # the other's holding it.
        assert set(holders) == {by_account[account_a].id, by_account[account_b].id}

    async def test_a_job_for_an_account_with_no_owner_is_skipped(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        settings_service = await _settings_service(migrated_db)
        cred_repo = ProviderCredentialRepository(migrated_db, encryption_key="")
        async with migrated_db.session() as session:
            account_id = await _seed_account(session, orders_enabled=True)
            await session.execute(
                text("DELETE FROM account_owners WHERE account_id = :id"), {"id": account_id},
            )
            folder_id = await _seed_folder(session, account_id=account_id)
            mail_id, key = await _seed_message(
                session, account_id=account_id, folder_id=folder_id, subject="Order NK-1 sent",
            )
            row = await _enqueue_mail_job(
                session, account_id=account_id, message_id=mail_id, msg_key=key,
            )

        await _handle_mail_job(row, migrated_db, cred_repo, settings_service)

        async with migrated_db.session() as session:
            job = (
                await session.execute(select(OrderJob).where(OrderJob.id == row["id"]))
            ).scalar_one()
            attached = (
                await session.execute(
                    select(OrderMail.id).where(OrderMail.account_id == account_id)
                )
            ).all()
        assert (job.outcome, job.last_error) == ("skipped", "no owner")
        assert attached == []

    async def test_candidates_and_lookup_see_only_the_callers_orders(
        self, migrated_db: DatabaseConnection, number: str,
    ) -> None:
        user_a, user_b, account_a, account_b = await _two_users(migrated_db)
        order_a = await _written_order(migrated_db, user_a, account_a, number=number)

        async def candidates(user_id: uuid.UUID, account_id: uuid.UUID) -> list[uuid.UUID]:
            async with migrated_db.session() as session:
                found = await find_candidates(
                    session, user_id=user_id, account_id=account_id, thread_id=None,
                    subject=f"Order {number}", from_addr="shop@example.com",
                    haystack_raw=f"Order number {number}",
                )
            return [c.order_id for c in found]

        assert order_a in await candidates(user_a, account_a)
        assert order_a not in await candidates(user_b, account_b)
        text_with_number = f"re: {number}"
        assert await OrderLookup(migrated_db, user_a).known(
            account_id=account_a, thread_id=None, text=text_with_number,
        ) == "number"
        assert await OrderLookup(migrated_db, user_b).known(
            account_id=account_b, thread_id=None, text=text_with_number,
        ) is None

    async def test_a_number_held_by_another_users_order_does_not_refuse_it(
        self, migrated_db: DatabaseConnection, number: str,
    ) -> None:
        user_a, user_b, account_a, account_b = await _two_users(migrated_db)
        order_a = await _written_order(migrated_db, user_a, account_a, number=number)
        order_b = await _written_order(migrated_db, user_b, account_b, number=number)
        same_user = await _written_order(migrated_db, user_a, account_a)
        async with migrated_db.session() as session:
            await repository.store_identifier_facts(
                session, same_user, [("order_number", number)],
            )
            held = {
                row.order_id for row in (
                    await session.execute(
                        select(OrderFact.order_id).where(OrderFact.value_norm == number.upper())
                    )
                ).all()
            }
        # B's order holds it beside A's; a second order of A's is refused.
        assert held == {order_a, order_b}


@pytest.mark.asyncio
class TestSweepsUseTheOwnersSettings:
    async def test_auto_close_follows_each_owners_own_days(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        service = await _settings_service(migrated_db)
        user_a, user_b, account_a, account_b = await _two_users(migrated_db)
        await service.update_for_user(
            "orders", user_a, {"auto_close_days": 30}, is_admin=False,
        )
        await service.update_for_user(
            "orders", user_b, {"auto_close_days": 0}, is_admin=False,
        )
        order_a = await _written_order(migrated_db, user_a, account_a, last_mail_age_days=40)
        order_b = await _written_order(migrated_db, user_b, account_b, last_mail_age_days=40)

        await auto_close_once(migrated_db, service, None)

        async with migrated_db.session() as session:
            states = dict(
                (await session.execute(
                    select(Order.id, Order.is_open).where(Order.id.in_((order_a, order_b)))
                )).all()
            )
        assert states == {order_a: False, order_b: True}

    async def test_catch_up_applies_the_callers_filter_patterns(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        service = await _settings_service(migrated_db)
        user_a, user_b, account_a, account_b = await _two_users(migrated_db)
        await service.update_for_user(
            "orders", user_a,
            {"filter": {"include": {"subject": ["zxqv-quokka"]}, "exclude": {}}},
            is_admin=False,
        )
        async with migrated_db.session() as session:
            for account_id in (account_a, account_b):
                folder_id = await _seed_folder(session, account_id=account_id)
                await _seed_message(
                    session, account_id=account_id, folder_id=folder_id,
                    subject="zxqv-quokka news", from_addr="friend@example.net",
                    received_at=_NOW - timedelta(days=1), body_text="Nothing else to see.",
                )

        for_a = await run_catch_up(
            migrated_db, service, user_id=user_a, account_id=account_a, days=7, dry_run=True,
        )
        for_b = await run_catch_up(
            migrated_db, service, user_id=user_b, account_id=account_b, days=7, dry_run=True,
        )

        assert (for_a.considered, for_a.passed) == (1, 1)
        assert (for_b.considered, for_b.passed) == (1, 0)

    async def test_the_write_call_uses_the_owners_language(
        self, migrated_db: DatabaseConnection, model_stub: ModelStub,
    ) -> None:
        service = await _stub_tasks(migrated_db, "write")
        user_a, user_b, account_a, account_b = await _two_users(migrated_db)
        await service.update_for_user("orders", user_a, {"language": "Klingon"}, is_admin=False)
        await service.update_for_user("orders", user_b, {"language": "Dothraki"}, is_admin=False)
        order_b = await _written_order(migrated_db, user_b, account_b)
        model_stub.answers["write"] = {
            "merchant": "Shop", "subject": "Thing", "status": "ordered", "icon": "package",
            "summary": "A thing.", "open": True, "kind": "order", "facts": [],
            "expected_until": None, "event_start": None, "event_end": None,
        }
        async with migrated_db.session() as session:
            await session.execute(
                text("UPDATE orders SET written_at = NULL WHERE id = :id"), {"id": order_b},
            )
            await repository.enqueue_write_job(session, order_b, priority=0)
            job = (
                await session.execute(
                    select(OrderJob).where(OrderJob.kind == "write", OrderJob.order_id == order_b)
                )
            ).scalar_one()

        await _handle_write_job(
            {"id": job.id, "kind": "write", "order_id": order_b},
            migrated_db, ProviderCredentialRepository(migrated_db, encryption_key=""), service,
        )

        prompt = model_stub.last("write")["system_prompt"]
        assert "Dothraki" in prompt and "Klingon" not in prompt


@pytest.fixture()
def app() -> FastAPI:
    application = FastAPI()
    application.include_router(router)
    return application


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    """One portal for the whole test: the shared database's connections are
    bound to the loop that opened them."""
    with TestClient(app) as c:
        yield c


class TestRoutesActForTheCaller:
    def test_one_user_cannot_list_read_change_or_delete_anothers_order(
        self, app: FastAPI, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        user_a, user_b, account_a, account_b = client.portal.call(_two_users, migrated_db)
        order_a = client.portal.call(_written_order, migrated_db, user_a, account_a)
        order_b = client.portal.call(_written_order, migrated_db, user_b, account_b)
        act_as(app, user_scope(user_a, [account_a]))

        with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
            listed = {item["id"] for item in client.get("/orders").json()["items"]}
            read = client.get(f"/orders/{order_b}")
            patched = client.patch(f"/orders/{order_b}", json={"is_favorite": True})
            rewritten = client.post(f"/orders/{order_b}/rewrite")
            merged = client.post(f"/orders/{order_a}/merge", json={"into": str(order_b)})
            merged_away = client.post(f"/orders/{order_b}/merge", json={"into": str(order_a)})
            deleted = client.delete(f"/orders/{order_b}")
            mine = client.get(f"/orders/{order_a}")
            swept = client.post(
                "/orders/catch-up",
                json={"account_id": str(account_b), "days": 7, "dry_run": True},
            )

        assert str(order_a) in listed and str(order_b) not in listed
        refused = (read, patched, rewritten, merged, merged_away, deleted)
        assert [r.status_code for r in refused] == [404] * 6
        assert mine.status_code == 200
        assert swept.status_code == 403

        async def favorite() -> bool:
            async with migrated_db.session() as session:
                return bool(
                    (await session.execute(select(Order.is_favorite).where(Order.id == order_b)))
                    .scalar_one()
                )

        assert client.portal.call(favorite) is False

    def test_uploaded_files_stay_with_their_user_and_never_join_a_foreign_order(
        self, app: FastAPI, client: TestClient, migrated_db: DatabaseConnection,
        orders_config: None, number: str,
    ) -> None:
        user_a, user_b, account_a, account_b = client.portal.call(_two_users, migrated_db)
        order_b = client.portal.call(_written_order, migrated_db, user_b, account_b)
        pdf = ticket_pdf(number)

        def upload(user_id: uuid.UUID, **form: Any) -> Any:
            act_as(app, user_scope(user_id))
            with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
                return client.post(
                    "/orders/files", files={"file": ("t.pdf", pdf, "application/pdf")}, data=form,
                )

        first = upload(user_a)
        into_foreign = upload(user_a, order_id=str(order_b))
        second = upload(user_b)
        act_as(app, user_scope(user_a))
        with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
            status_of_foreign = client.get(f"/orders/files/{second.json()['file_id']}")

        assert first.status_code == 202 and second.status_code == 202
        assert into_foreign.status_code == 404
        # The same bytes from the other user are their own file, not A's copy.
        assert first.json()["file_id"] != second.json()["file_id"]
        assert status_of_foreign.status_code == 404
        first_id = uuid.UUID(first.json()["file_id"])
        second_id = uuid.UUID(second.json()["file_id"])

        async def owners() -> dict[uuid.UUID, uuid.UUID | None]:
            async with migrated_db.session() as session:
                return dict(
                    (await session.execute(
                        select(OrderFile.id, OrderFile.user_id).where(
                            OrderFile.id.in_((first_id, second_id))
                        )
                    )).all()
                )

        assert client.portal.call(owners) == {first_id: user_a, second_id: user_b}

    def test_a_placed_file_downloads_and_deletes_for_its_user_only(
        self, app: FastAPI, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        user_a, user_b, account_a, account_b = client.portal.call(_two_users, migrated_db)
        order_b = client.portal.call(_written_order, migrated_db, user_b, account_b)
        file_id = uuid.uuid4()

        async def place() -> None:
            async with migrated_db.session() as session:
                await session.execute(
                    text(
                        "INSERT INTO order_files (id, order_id, filename, content_type, "
                        "size_bytes, sha256, data, status, user_id) VALUES (:id, :order, "
                        "'b.pdf', 'application/pdf', 3, 'x', '\\x616263', 'attached', :user)"
                    ),
                    {"id": file_id, "order": order_b, "user": user_b},
                )

        client.portal.call(place)
        path = f"/orders/{order_b}/files/{file_id}"

        with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
            act_as(app, user_scope(user_a))
            foreign_get, foreign_delete = client.get(path), client.delete(path)
            act_as(app, user_scope(user_b))
            own_get = client.get(path)

        assert (foreign_get.status_code, foreign_delete.status_code) == (404, 404)
        assert (own_get.status_code, own_get.content) == (200, b"abc")


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_a_file_job_never_places_a_file_into_another_users_order(
    migrated_db: DatabaseConnection, orders_config: None,
) -> None:
    service = await _settings_service(migrated_db)
    user_a, user_b, _account_a, account_b = await _two_users(migrated_db)
    order_b = await _written_order(migrated_db, user_b, account_b)
    file_id = uuid.uuid4()
    async with migrated_db.session() as session:
        await session.execute(
            text(
                "INSERT INTO order_files (id, order_id, filename, content_type, size_bytes, "
                "sha256, data, status, user_id) VALUES (:id, :order, 'n.txt', 'text/plain', 5, "
                "'x', '\\x68656c6c6f', 'pending', :user)"
            ),
            {"id": file_id, "order": order_b, "user": user_a},
        )
        job = await session.execute(
            pg_insert(OrderJob).values(kind="file", file_id=file_id, origin="upload", priority=5)
            .returning(OrderJob.id)
        )
        job_id = job.scalar_one()

    await _handle_file_job(
        {"id": job_id, "kind": "file", "file_id": file_id, "origin": "upload"},
        migrated_db, ProviderCredentialRepository(migrated_db, encryption_key=""), service,
    )

    async with migrated_db.session() as session:
        outcome = (
            await session.execute(select(OrderJob.outcome).where(OrderJob.id == job_id))
        ).scalar_one()
        status = (
            await session.execute(select(OrderFile.status).where(OrderFile.id == file_id))
        ).scalar_one()
    assert (outcome, status) == ("skipped", "pending")


@pytest.mark.asyncio
class TestGivingAnAccountAwayMovesItsOrders:
    async def test_an_order_of_only_that_account_moves_a_mixed_one_loses_its_mail(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        user_a, user_b, account_a, account_b = await _two_users(migrated_db)
        async with migrated_db.session() as session:
            other_of_a = await _seed_account(session, orders_enabled=True, owner_id=user_a)
        pure = await _written_order(migrated_db, user_a, account_a)
        mixed = await _written_order(
            migrated_db, user_a, account_a, extra_account_id=other_of_a,
        )
        only_theirs = await _written_order(migrated_db, user_a, other_of_a)

        async with migrated_db.session() as session:
            changes = await reassign_account(session, account_a, user_b)

        async with migrated_db.session() as session:
            owners = dict(
                (await session.execute(
                    select(Order.id, Order.user_id).where(Order.id.in_((pure, mixed, only_theirs)))
                )).all()
            )
            mixed_accounts = (
                await session.execute(
                    select(OrderMail.account_id).where(OrderMail.order_id == mixed)
                )
            ).scalars().all()
        assert owners == {pure: user_b, mixed: user_a, only_theirs: user_a}
        assert mixed_accounts == [other_of_a]
        assert sorted((str(u), str(o), c) for u, o, c in changes) == sorted([
            (str(user_a), str(pure), "deleted"), (str(user_b), str(pure), "updated"),
            (str(user_a), str(mixed), "updated"),
        ])

    async def test_an_owner_keeps_what_is_theirs_when_the_account_is_given_to_them(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        user_a, _user_b, account_a, _account_b = await _two_users(migrated_db)
        order = await _written_order(migrated_db, user_a, account_a)

        async with migrated_db.session() as session:
            changes = await reassign_account(session, account_a, user_a)

        assert changes == []
        async with migrated_db.session() as session:
            owner = (
                await session.execute(select(Order.user_id).where(Order.id == order))
            ).scalar_one()
        assert owner == user_a


@pytest.mark.asyncio
async def test_the_owner_row_still_reads_the_orders_stamped_for_it(
    migrated_db: DatabaseConnection, number: str,
) -> None:
    account = uuid.uuid4()
    order = await _written_order(migrated_db, OWNER_USER_ID, account, number=number)
    async with migrated_db.session() as session:
        found = await find_candidates(
            session, user_id=OWNER_USER_ID, account_id=account, thread_id=None,
            subject=f"Order {number}", from_addr="shop@example.com",
            haystack_raw=f"Order number {number}",
        )
    assert order in [c.order_id for c in found]
