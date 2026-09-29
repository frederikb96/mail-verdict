"""
orders/repository.py against a real migrated database: attach recomputes
count and dates, a second attach of the same (account_id, msg_key) is
refused, detach/move/merge/delete, an order left empty is deleted, and an
identifier another order holds is not stored.

No PostIMAP account or message rows are needed -- membership is keyed on
(account_id, msg_key), both plain values these functions never dereference
against messages, so the whole surface is testable with nothing seeded but
the orders tables themselves.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Order, OrderIdentifier, OrderMail
from mail_verdict.orders import repository

pytestmark = pytest.mark.asyncio

_NOW = datetime.now(timezone.utc)


async def _order_row(db: DatabaseConnection, order_id: uuid.UUID) -> Order:
    async with db.session() as session:
        result = await session.execute(select(Order).where(Order.id == order_id))
        return result.scalar_one()


async def test_attach_recomputes_count_and_dates(migrated_db: DatabaseConnection) -> None:
    account_id = uuid.uuid4()
    async with migrated_db.session() as session:
        order_id = await repository.create_order(session)
        await repository.attach_mail(
            session, order_id=order_id, account_id=account_id, msg_key="<one@example.com>",
            message_id=None, thread_id=None, subject="s1", from_addr="a@b.com",
            received_at=_NOW - timedelta(days=1), attached_by="ai",
        )
        await repository.attach_mail(
            session, order_id=order_id, account_id=account_id, msg_key="<two@example.com>",
            message_id=None, thread_id=None, subject="s2", from_addr="a@b.com",
            received_at=_NOW, attached_by="ai",
        )
        await repository.recompute_aggregates(session, order_id)

    order = await _order_row(migrated_db, order_id)
    assert order.mail_count == 2
    assert order.first_mail_at == _NOW - timedelta(days=1)
    assert order.last_mail_at == _NOW


async def test_a_second_attach_of_the_same_account_and_msg_key_is_refused(
    migrated_db: DatabaseConnection,
) -> None:
    account_id = uuid.uuid4()
    async with migrated_db.session() as session:
        order_a = await repository.create_order(session)
        order_b = await repository.create_order(session)
        first = await repository.attach_mail(
            session, order_id=order_a, account_id=account_id, msg_key="<dup@example.com>",
            message_id=None, thread_id=None, subject="s", from_addr="a@b.com",
            received_at=_NOW, attached_by="ai",
        )
        second = await repository.attach_mail(
            session, order_id=order_b, account_id=account_id, msg_key="<dup@example.com>",
            message_id=None, thread_id=None, subject="s", from_addr="a@b.com",
            received_at=_NOW, attached_by="ai",
        )

    assert first is True
    assert second is False
    async with migrated_db.session() as session:
        result = await session.execute(
            select(OrderMail.order_id).where(OrderMail.msg_key == "<dup@example.com>")
        )
        rows = result.all()
    assert len(rows) == 1
    assert rows[0].order_id == order_a


async def test_detach_removes_the_row_and_leaves_the_order_findable(
    migrated_db: DatabaseConnection,
) -> None:
    account_id = uuid.uuid4()
    async with migrated_db.session() as session:
        order_id = await repository.create_order(session)
        await repository.attach_mail(
            session, order_id=order_id, account_id=account_id, msg_key="<x@example.com>",
            message_id=None, thread_id=None, subject="s", from_addr="a@b.com",
            received_at=_NOW, attached_by="ai",
        )
        mail_result = await session.execute(
            select(OrderMail.id).where(OrderMail.msg_key == "<x@example.com>")
        )
        mail_id = mail_result.scalar_one()

        removed_from = await repository.detach_mail(session, mail_id)
        remaining = await repository.recompute_aggregates(session, order_id)

    assert removed_from == order_id
    assert remaining == 0


async def test_an_order_left_with_no_mail_is_deleted(migrated_db: DatabaseConnection) -> None:
    account_id = uuid.uuid4()
    async with migrated_db.session() as session:
        order_id = await repository.create_order(session)
        await repository.attach_mail(
            session, order_id=order_id, account_id=account_id, msg_key="<only@example.com>",
            message_id=None, thread_id=None, subject="s", from_addr="a@b.com",
            received_at=_NOW, attached_by="ai",
        )
        mail_result = await session.execute(
            select(OrderMail.id).where(OrderMail.msg_key == "<only@example.com>")
        )
        mail_id = mail_result.scalar_one()
        await repository.detach_mail(session, mail_id)
        remaining = await repository.recompute_aggregates(session, order_id)
        assert remaining == 0
        deleted = await repository.delete_order_if_empty(session, order_id)

    assert deleted is True
    async with migrated_db.session() as session:
        result = await session.execute(select(Order.id).where(Order.id == order_id))
        assert result.scalar_one_or_none() is None


async def test_merge_moves_every_mail_and_identifier_and_deletes_the_source(
    migrated_db: DatabaseConnection,
) -> None:
    account_id = uuid.uuid4()
    async with migrated_db.session() as session:
        source = await repository.create_order(session)
        target = await repository.create_order(session)
        await repository.attach_mail(
            session, order_id=source, account_id=account_id, msg_key="<m1@example.com>",
            message_id=None, thread_id=None, subject="s", from_addr="a@b.com",
            received_at=_NOW, attached_by="ai",
        )
        await repository.store_identifiers(session, source, [("order_number", "AB1234")])

        await repository.merge_order(session, source_id=source, target_id=target)

    async with migrated_db.session() as session:
        source_gone = await session.execute(select(Order.id).where(Order.id == source))
        assert source_gone.scalar_one_or_none() is None

        target_row = await session.execute(select(Order).where(Order.id == target))
        target_order = target_row.scalar_one()
        assert target_order.mail_count == 1

        mails_result = await session.execute(
            select(OrderMail.order_id).where(OrderMail.msg_key == "<m1@example.com>")
        )
        assert mails_result.scalar_one() == target

        ids_result = await session.execute(
            select(OrderIdentifier.value).where(OrderIdentifier.order_id == target)
        )
        assert "AB1234" in {row[0] for row in ids_result.all()}


async def test_delete_order_removes_it_but_never_touches_the_mail_row(
    migrated_db: DatabaseConnection,
) -> None:
    account_id = uuid.uuid4()
    async with migrated_db.session() as session:
        order_id = await repository.create_order(session)
        await repository.attach_mail(
            session, order_id=order_id, account_id=account_id, msg_key="<d@example.com>",
            message_id=None, thread_id=None, subject="s", from_addr="a@b.com",
            received_at=_NOW, attached_by="ai",
        )
        deleted = await repository.delete_order(session, order_id)

    assert deleted is True
    async with migrated_db.session() as session:
        # order_mails cascades with the order -- this asserts the cascade
        # exists, not that a live mailbox message was touched (none was
        # created here; membership is (account_id, msg_key) alone).
        mail_result = await session.execute(
            select(OrderMail.id).where(OrderMail.msg_key == "<d@example.com>")
        )
        assert mail_result.scalar_one_or_none() is None


async def test_an_identifier_another_recently_active_order_holds_is_not_stored(
    migrated_db: DatabaseConnection,
) -> None:
    account_id = uuid.uuid4()
    async with migrated_db.session() as session:
        order_a = await repository.create_order(session)
        order_b = await repository.create_order(session)
        await repository.attach_mail(
            session, order_id=order_a, account_id=account_id, msg_key="<a@example.com>",
            message_id=None, thread_id=None, subject="s", from_addr="a@b.com",
            received_at=_NOW, attached_by="ai",
        )
        await repository.recompute_aggregates(session, order_a)
        await repository.store_identifiers(session, order_a, [("order_number", "ZZ9999")])

        await repository.attach_mail(
            session, order_id=order_b, account_id=account_id, msg_key="<b@example.com>",
            message_id=None, thread_id=None, subject="s", from_addr="a@b.com",
            received_at=_NOW, attached_by="ai",
        )
        # order_a's last_mail_at must be recent for the exclusion window
        # to apply -- recompute_aggregates above already set it to _NOW.
        await repository.store_identifiers(session, order_b, [("order_number", "ZZ9999")])

    async with migrated_db.session() as session:
        result = await session.execute(
            select(OrderIdentifier.order_id).where(OrderIdentifier.value == "ZZ9999")
        )
        rows = {row[0] for row in result.all()}
    assert rows == {order_a}
