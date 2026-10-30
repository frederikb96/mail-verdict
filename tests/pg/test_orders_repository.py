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
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Order, OrderDocument, OrderFact, OrderFile, OrderMail
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
        await repository.store_identifier_facts(session, source, [("order_number", "AB1234")])

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
            select(OrderFact.value).where(OrderFact.order_id == target)
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
        await repository.store_identifier_facts(session, order_a, [("order_number", "ZZ9999")])

        await repository.attach_mail(
            session, order_id=order_b, account_id=account_id, msg_key="<b@example.com>",
            message_id=None, thread_id=None, subject="s", from_addr="a@b.com",
            received_at=_NOW, attached_by="ai",
        )
        # order_a's last_mail_at must be recent for the exclusion window
        # to apply -- recompute_aggregates above already set it to _NOW.
        await repository.store_identifier_facts(session, order_b, [("order_number", "ZZ9999")])

    async with migrated_db.session() as session:
        result = await session.execute(
            select(OrderFact.order_id).where(OrderFact.value == "ZZ9999")
        )
        rows = {row[0] for row in result.all()}
    assert rows == {order_a}


async def test_enqueue_write_job_is_a_no_op_when_one_is_already_pending(
    migrated_db: DatabaseConnection,
) -> None:
    """uq_order_jobs_write is a partial unique INDEX, not a table
    CONSTRAINT (it carries a WHERE clause, which a constraint cannot) --
    resolving its ON CONFLICT arbiter by constraint name raises
    UndefinedObjectError against a real Postgres, which only a real
    database (never the ORM alone) catches."""
    from mail_verdict.database.models import OrderJob

    async with migrated_db.session() as session:
        order_id = await repository.create_order(session)
        await repository.enqueue_write_job(session, order_id, priority=50)
        await repository.enqueue_write_job(session, order_id, priority=10)

    async with migrated_db.session() as session:
        result = await session.execute(
            select(OrderJob.priority).where(
                OrderJob.order_id == order_id, OrderJob.kind == "write",
            )
        )
        rows = result.all()
    assert len(rows) == 1
    assert rows[0].priority == 50


async def _attach_mail_with_pdf(
    session: AsyncSession, order_id: uuid.UUID, account_id: uuid.UUID, msg_key: str,
    received_at: datetime,
) -> uuid.UUID:
    """One mail whose PDF was extracted and whose Aztec code became a fact;
    returns the document id."""
    await repository.attach_mail(
        session, order_id=order_id, account_id=account_id, msg_key=msg_key,
        message_id=None, thread_id=None, subject="s", from_addr="a@b.com",
        received_at=received_at, attached_by="ai",
    )
    document_id = (
        await session.execute(
            text(
                "INSERT INTO order_documents (order_id, source, account_id, msg_key, "
                "attachment_id, filename, method) VALUES (:order_id, 'mail', :account_id, "
                ":msg_key, :attachment_id, 'ticket.pdf', 'text_layer') RETURNING id"
            ),
            {
                "order_id": order_id, "account_id": account_id, "msg_key": msg_key,
                "attachment_id": uuid.uuid4(),
            },
        )
    ).scalar_one()
    await session.execute(
        text(
            "INSERT INTO order_facts (order_id, kind, label, value, document_id, barcode_index) "
            "VALUES (:order_id, 'code', 'Ticketcode', '', :document_id, 1)"
        ),
        {"order_id": order_id, "document_id": document_id},
    )
    return uuid.UUID(str(document_id))


async def _file_row(session: AsyncSession, order_id: uuid.UUID, created_at: datetime) -> uuid.UUID:
    file_id = (
        await session.execute(
            text(
                "INSERT INTO order_files (order_id, filename, size_bytes, sha256, data, status, "
                "created_at) VALUES (:order_id, 'scan.pdf', 3, 'abc', '\\x010203', 'attached', "
                ":created_at) RETURNING id"
            ),
            {"order_id": order_id, "created_at": created_at},
        )
    ).scalar_one()
    return uuid.UUID(str(file_id))


async def test_activity_covers_files_and_an_order_with_only_a_file_is_not_empty(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        order_id = await repository.create_order(session)
        await _file_row(session, order_id, _NOW - timedelta(days=3))
        assert await repository.recompute_aggregates(session, order_id) == 1
        assert await repository.delete_order_if_empty(session, order_id) is False

        await repository.attach_mail(
            session, order_id=order_id, account_id=uuid.uuid4(), msg_key="<act@example.com>",
            message_id=None, thread_id=None, subject="s", from_addr="a@b.com",
            received_at=_NOW, attached_by="ai",
        )
        assert await repository.recompute_aggregates(session, order_id) == 2

    order = await _order_row(migrated_db, order_id)
    assert (order.mail_count, order.file_count) == (1, 1)
    assert order.first_mail_at == order.last_mail_at == _NOW
    assert order.first_activity_at == _NOW - timedelta(days=3)
    assert order.last_activity_at == _NOW


async def test_detaching_a_mail_deletes_its_documents_and_code_facts(
    migrated_db: DatabaseConnection,
) -> None:
    account_id = uuid.uuid4()
    async with migrated_db.session() as session:
        order_id = await repository.create_order(session)
        kept = await _attach_mail_with_pdf(session, order_id, account_id, "<keep@x.com>", _NOW)
        await _attach_mail_with_pdf(session, order_id, account_id, "<drop@x.com>", _NOW)
        drop_key = (
            await session.execute(select(OrderMail.id).where(OrderMail.msg_key == "<drop@x.com>"))
        ).scalar_one()

        await repository.detach_mail(session, drop_key)

    async with migrated_db.session() as session:
        documents = (
            await session.execute(
                select(OrderDocument.id).where(OrderDocument.order_id == order_id)
            )
        ).scalars().all()
        codes = (
            await session.execute(
                select(OrderFact.document_id).where(OrderFact.order_id == order_id)
            )
        ).scalars().all()
    assert list(documents) == [kept]
    assert list(codes) == [kept]


async def test_moving_a_mail_moves_its_documents_and_code_facts_with_it(
    migrated_db: DatabaseConnection,
) -> None:
    account_id = uuid.uuid4()
    async with migrated_db.session() as session:
        source = await repository.create_order(session)
        target = await repository.create_order(session)
        document_id = await _attach_mail_with_pdf(session, source, account_id, "<mv@x.com>", _NOW)
        mail_key = (
            await session.execute(select(OrderMail.id).where(OrderMail.msg_key == "<mv@x.com>"))
        ).scalar_one()

        await repository.detach_mail(session, mail_key, move_documents_to=target)

    async with migrated_db.session() as session:
        document_order = (
            await session.execute(
                select(OrderDocument.order_id).where(OrderDocument.id == document_id)
            )
        ).scalar_one()
        code_order = (
            await session.execute(
                select(OrderFact.order_id).where(OrderFact.document_id == document_id)
            )
        ).scalar_one()
    assert document_order == code_order == target


async def test_merge_moves_documents_files_and_code_facts_and_drops_display_facts(
    migrated_db: DatabaseConnection,
) -> None:
    account_id = uuid.uuid4()
    async with migrated_db.session() as session:
        source = await repository.create_order(session)
        target = await repository.create_order(session)
        document_id = await _attach_mail_with_pdf(session, source, account_id, "<mg@x.com>", _NOW)
        file_id = await _file_row(session, source, _NOW)
        await session.execute(
            text(
                "INSERT INTO order_facts (order_id, kind, label, value) "
                "VALUES (:id, 'place', 'Station', 'Aachen Hbf')"
            ),
            {"id": source},
        )

        await repository.merge_order(session, source_id=source, target_id=target)

    async with migrated_db.session() as session:
        assert (
            await session.execute(
                select(OrderDocument.order_id).where(OrderDocument.id == document_id)
            )
        ).scalar_one() == target
        assert (
            await session.execute(select(OrderFile.order_id).where(OrderFile.id == file_id))
        ).scalar_one() == target
        kinds = (
            await session.execute(select(OrderFact.kind).where(OrderFact.order_id == target))
        ).scalars().all()
    assert list(kinds) == ["code"]
    order = await _order_row(migrated_db, target)
    assert (order.mail_count, order.file_count) == (1, 1)


async def test_deleting_an_order_removes_its_facts_documents_and_files(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        order_id = await repository.create_order(session)
        document_id = await _attach_mail_with_pdf(
            session, order_id, uuid.uuid4(), "<del@x.com>", _NOW,
        )
        file_id = await _file_row(session, order_id, _NOW)

        assert await repository.delete_order(session, order_id) is True

    async with migrated_db.session() as session:
        assert (
            await session.execute(select(OrderDocument.id).where(OrderDocument.id == document_id))
        ).first() is None
        assert (
            await session.execute(select(OrderFile.id).where(OrderFile.id == file_id))
        ).first() is None
        assert (
            await session.execute(select(OrderFact.id).where(OrderFact.order_id == order_id))
        ).first() is None


async def test_deleting_an_account_removes_its_mail_documents_and_keeps_a_file_only_order(
    migrated_db: DatabaseConnection,
) -> None:
    gone_account = uuid.uuid4()
    async with migrated_db.session() as session:
        mixed = await repository.create_order(session)
        only_mail = await repository.create_order(session)
        await _attach_mail_with_pdf(session, mixed, gone_account, "<acct1@x.com>", _NOW)
        await _file_row(session, mixed, _NOW)
        await repository.recompute_aggregates(session, mixed)
        await _attach_mail_with_pdf(session, only_mail, gone_account, "<acct2@x.com>", _NOW)
        await repository.recompute_aggregates(session, only_mail)

        changes = await repository.delete_for_account(session, gone_account)

    assert sorted(changes, key=str) == sorted([(mixed, "updated"), (only_mail, "deleted")], key=str)
    async with migrated_db.session() as session:
        assert (
            await session.execute(select(OrderDocument.id).where(OrderDocument.order_id == mixed))
        ).first() is None
        remaining = await session.execute(
            select(Order.mail_count, Order.file_count).where(Order.id == mixed)
        )
        assert tuple(remaining.one()) == (0, 1)


async def test_a_user_set_kind_survives_and_a_model_kind_lands(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        order_id = await repository.create_order(session)
        await repository.write_order_text(
            session, order_id, merchant="m", subject="s", status="x", is_open=True,
            icon="train", summary="s", model="fake", kind="ticket",
            event_start=_NOW.date(), event_end=_NOW.date() + timedelta(days=1),
        )
    order = await _order_row(migrated_db, order_id)
    assert (order.kind, order.kind_set_by) == ("ticket", "ai")
    assert order.event_end == _NOW.date() + timedelta(days=1)

    async with migrated_db.session() as session:
        await repository.update_controls(session, order_id, kind="other")
        await repository.write_order_text(
            session, order_id, merchant="m", subject="s", status="x", is_open=True,
            icon="train", summary="s", model="fake", kind="ticket", event_start=None,
        )
    order = await _order_row(migrated_db, order_id)
    assert (order.kind, order.kind_set_by) == ("other", "user")
    assert order.event_start is None
