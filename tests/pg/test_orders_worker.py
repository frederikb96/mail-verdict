"""
The orders queue's worker against a real database, with the "fake"
provider standing in for a model call: two enabled accounts' mail ending
in one order while a disabled account's mail is refused, mail delivered
in the same second landing in one order, a re-run of an already-handled
job changing nothing, and the two thread-bypass paths (a reply landing in
the inbox, and one sent from this account) joining a conversation's order
without either mail's subject saying anything about orders.
"""

from __future__ import annotations

import itertools
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Order, OrderJob, OrderMail
from mail_verdict.orders import repository
from mail_verdict.orders.intake import enqueue_thread_follow_up
from mail_verdict.orders.lookup import OrderLookup
from mail_verdict.orders.worker import _handle_mail_job
from mail_verdict.pipeline.context import BoundLog, MessageHistory, RunContext
from mail_verdict.pipeline.message_view import FolderView, MessageView
from mail_verdict.pipeline.stages.orders import OrdersConfig, OrdersStage
from mail_verdict.postimap.listener import PostimapEvent
from mail_verdict.settings.credentials import ProviderCredentialRepository
from mail_verdict.settings.service import SettingsService

pytestmark = pytest.mark.asyncio

_imap_uid_counter = itertools.count(1)
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


async def _settings_service(db: DatabaseConnection) -> SettingsService:
    service = SettingsService(db)
    await service.load()
    await service.update("ai", {"provider": "fake"})
    return service


async def _seed_account(
    session: AsyncSession, *, orders_enabled: bool | None,
) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    if orders_enabled is not None:
        await session.execute(
            text(
                "INSERT INTO account_prefs (account_id, orders_enabled) "
                "VALUES (:id, :enabled)"
            ),
            {"id": account_id, "enabled": orders_enabled},
        )
    return account_id


async def _seed_folder(
    session: AsyncSession, *, account_id: uuid.UUID, special_use: str | None = None,
) -> uuid.UUID:
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use) "
            "VALUES (:id, :account_id, :name, :special_use)"
        ),
        {
            "id": folder_id, "account_id": account_id,
            "name": special_use or "INBOX", "special_use": special_use,
        },
    )
    return folder_id


async def _seed_message(
    session: AsyncSession, *, account_id: uuid.UUID, folder_id: uuid.UUID,
    subject: str, from_addr: str = "shop@example.com", received_at: datetime = _NOW,
    thread_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, str]:
    mail_id = uuid.uuid4()
    header = f"<{uuid.uuid4()}@example.com>"
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, from_addr, "
            "subject, body_text, received_at, size_bytes, is_seen) "
            "VALUES (:id, :account_id, :folder_id, :uid, :thread_id, :message_id, :from_addr, "
            ":subject, :body_text, :received_at, 512, false)"
        ),
        {
            "id": mail_id, "account_id": account_id, "folder_id": folder_id,
            "uid": next(_imap_uid_counter), "thread_id": thread_id or uuid.uuid4(),
            "message_id": header, "from_addr": from_addr, "subject": subject,
            "body_text": "Thanks for your order. See you soon.",
            "received_at": received_at,
        },
    )
    return mail_id, header


async def _enqueue_mail_job(
    session: AsyncSession, *, account_id: uuid.UUID, message_id: uuid.UUID, msg_key: str,
    origin: str = "live", filter_reason: str = "subject",
) -> dict[str, object]:
    await repository.enqueue_mail_job(
        session, account_id=account_id, msg_key=msg_key, message_id=message_id,
        origin=origin, priority=0, filter_reason=filter_reason, next_attempt_at=_NOW,
    )
    row = (
        await session.execute(
            select(OrderJob).where(
                OrderJob.account_id == account_id, OrderJob.msg_key == msg_key,
                OrderJob.kind == "mail",
            )
        )
    ).scalar_one()
    return {
        "id": row.id, "kind": "mail", "account_id": row.account_id, "msg_key": row.msg_key,
        "message_id": row.message_id, "origin": row.origin,
        "filter_reason": row.filter_reason,
    }


async def _order_mails(db: DatabaseConnection, order_id: uuid.UUID) -> list[OrderMail]:
    async with db.session() as session:
        result = await session.execute(select(OrderMail).where(OrderMail.order_id == order_id))
        return list(result.scalars().all())


class TestTwoAccountsFeedOneOrder:
    async def test_two_enabled_accounts_bundle_into_one_order_a_disabled_ones_is_refused(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        settings_service = await _settings_service(migrated_db)
        cred_repo = ProviderCredentialRepository(migrated_db, encryption_key="")

        async with migrated_db.session() as session:
            account_a = await _seed_account(session, orders_enabled=True)
            account_b = await _seed_account(session, orders_enabled=True)
            account_c = await _seed_account(session, orders_enabled=False)
            folder_a = await _seed_folder(session, account_id=account_a)
            folder_b = await _seed_folder(session, account_id=account_b)
            folder_c = await _seed_folder(session, account_id=account_c)
            mail_a, key_a = await _seed_message(
                session, account_id=account_a, folder_id=folder_a,
                subject="Your order NK-48213 confirmed",
            )
            mail_b, key_b = await _seed_message(
                session, account_id=account_b, folder_id=folder_b,
                subject="NK-48213 has shipped", received_at=_NOW + timedelta(minutes=5),
            )
            mail_c, key_c = await _seed_message(
                session, account_id=account_c, folder_id=folder_c,
                subject="Order NK-48213 update",
            )
            row_a = await _enqueue_mail_job(
                session, account_id=account_a, message_id=mail_a, msg_key=key_a,
            )
            row_b = await _enqueue_mail_job(
                session, account_id=account_b, message_id=mail_b, msg_key=key_b,
            )
            row_c = await _enqueue_mail_job(
                session, account_id=account_c, message_id=mail_c, msg_key=key_c,
            )

        await _handle_mail_job(row_a, migrated_db, cred_repo, settings_service)
        await _handle_mail_job(row_b, migrated_db, cred_repo, settings_service)
        result_c = await _handle_mail_job(row_c, migrated_db, cred_repo, settings_service)

        assert result_c is None
        async with migrated_db.session() as session:
            job_c = (
                await session.execute(select(OrderJob).where(OrderJob.id == row_c["id"]))
            ).scalar_one()
            assert job_c.outcome == "skipped"
            assert job_c.last_error == "orders disabled"

            orders = (await session.execute(select(Order))).scalars().all()
            assert len(orders) == 1
            order = orders[0]
            assert order.mail_count == 2

            mails = (
                await session.execute(select(OrderMail).where(OrderMail.order_id == order.id))
            ).scalars().all()
        assert {m.account_id for m in mails} == {account_a, account_b}
        assert account_c not in {m.account_id for m in mails}


class TestSameSecondAndRerun:
    async def test_two_mails_in_the_same_second_end_in_one_order(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        settings_service = await _settings_service(migrated_db)
        cred_repo = ProviderCredentialRepository(migrated_db, encryption_key="")

        async with migrated_db.session() as session:
            account_id = await _seed_account(session, orders_enabled=True)
            folder_id = await _seed_folder(session, account_id=account_id)
            same_instant = _NOW.replace(microsecond=0)
            mail_1, key_1 = await _seed_message(
                session, account_id=account_id, folder_id=folder_id,
                subject="Order PQ-99120 confirmed", received_at=same_instant,
            )
            mail_2, key_2 = await _seed_message(
                session, account_id=account_id, folder_id=folder_id,
                subject="PQ-99120 shipping update", received_at=same_instant,
            )
            row_1 = await _enqueue_mail_job(
                session, account_id=account_id, message_id=mail_1, msg_key=key_1,
            )
            row_2 = await _enqueue_mail_job(
                session, account_id=account_id, message_id=mail_2, msg_key=key_2,
            )

        await _handle_mail_job(row_1, migrated_db, cred_repo, settings_service)
        await _handle_mail_job(row_2, migrated_db, cred_repo, settings_service)

        async with migrated_db.session() as session:
            orders = (
                await session.execute(
                    select(OrderMail.order_id.distinct()).where(
                        OrderMail.account_id == account_id,
                    )
                )
            ).scalars().all()
        assert len(orders) == 1

    async def test_a_finished_job_run_again_changes_nothing(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        settings_service = await _settings_service(migrated_db)
        cred_repo = ProviderCredentialRepository(migrated_db, encryption_key="")

        async with migrated_db.session() as session:
            account_id = await _seed_account(session, orders_enabled=True)
            folder_id = await _seed_folder(session, account_id=account_id)
            mail_id, key = await _seed_message(
                session, account_id=account_id, folder_id=folder_id,
                subject="Order RS-77410 confirmed",
            )
            row = await _enqueue_mail_job(
                session, account_id=account_id, message_id=mail_id, msg_key=key,
            )

        await _handle_mail_job(row, migrated_db, cred_repo, settings_service)
        async with migrated_db.session() as session:
            order_id = (
                await session.execute(
                    select(OrderMail.order_id).where(OrderMail.account_id == account_id)
                )
            ).scalar_one()
        mails_after_first = await _order_mails(migrated_db, order_id)
        assert len(mails_after_first) == 1

        # Re-run the same job, standing in for a reclaimed/duplicated
        # worker claim -- see orders/worker.py's module docstring.
        result = await _handle_mail_job(row, migrated_db, cred_repo, settings_service)
        assert result is None

        async with migrated_db.session() as session:
            job = (
                await session.execute(select(OrderJob).where(OrderJob.id == row["id"]))
            ).scalar_one()
            assert job.outcome == "attached"
            # The rerun returns before ever deciding again -- its decision
            # column still holds the FIRST run's answer ("new purchase",
            # fake_decide's target="new" reason), never the "follow-up"
            # a second decide call would produce once its own number
            # identifier makes its own order a matching candidate.
            assert job.decision is not None
            assert job.decision["mail_kind"] == "new purchase"
            order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        assert order.mail_count == 1
        mails_after_rerun = await _order_mails(migrated_db, order_id)
        assert len(mails_after_rerun) == 1


def _view(
    *, subject: str, account_id: uuid.UUID, thread_id: uuid.UUID | None,
) -> MessageView:
    return MessageView(
        message_id=uuid.uuid4(), msg_key="<thread-bypass-test@example.com>",
        account_id=account_id,
        folder=FolderView(id=uuid.uuid4(), imap_name="INBOX", special_use=None),
        subject=subject, from_addr="shop@example.com", to_addrs=("me@example.com",), cc_addrs=(),
        headers={}, body="", body_truncated=False, size_bytes=0,
        received_at=_NOW, is_seen=False, is_flagged=False,
        is_draft=False, is_truncated=False, keywords=(), tags=(), attachment_types=(),
        has_attachments=False, thread_id=thread_id,
        body_text_raw="Just checking in, nothing to report", body_html_raw=None,
    )


class TestThreadBypassJoinsAnOrdersConversation:
    async def test_orders_stage_bypasses_the_filter_for_a_reply_in_an_orders_thread(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """A reply landing in the inbox, whose subject says nothing about
        an order, still joins the order because its thread already does
        -- OrdersStage's own bypass rule, against a real OrderLookup."""
        async with migrated_db.session() as session:
            account_id = await _seed_account(session, orders_enabled=True)
            order_id = await repository.create_order(session)
            thread_id = uuid.uuid4()
            await repository.attach_mail(
                session, order_id=order_id, account_id=account_id,
                msg_key="<original@example.com>", message_id=None, thread_id=thread_id,
                subject="Order NK-1 confirmed", from_addr="shop@example.com",
                received_at=_NOW, attached_by="ai",
            )

        stage = OrdersStage("orders", OrdersConfig())
        ctx = RunContext(
            run_id=uuid.uuid4(), account_id=account_id, origin="live", apply=True,
            settings={"orders": {"filter": {"include": {"subject": ["bestell", "order"]}}}},
            trace=(), facts={}, verdict=None, history=MessageHistory(has_ai_verdict=False),
            folders=None, neighbors=None, models=None,  # type: ignore[arg-type]
            log=BoundLog(logging.getLogger("test")),
            account_spam_enabled=False, account_orders_enabled=True,
            orders=OrderLookup(migrated_db),
        )
        outcome = await stage.execute(
            _view(subject="Re: hi", account_id=account_id, thread_id=thread_id), ctx,
        )

        assert outcome.matched is True
        assert len(outcome.effects) == 1
        assert outcome.effects[0].reason == "thread"

    async def test_intake_enqueues_a_sent_reply_whose_thread_belongs_to_an_order(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The outgoing half: a bare 'Re:' answer sent from this account,
        in Sent, whose subject also says nothing about an order."""
        async with migrated_db.session() as session:
            account_id = await _seed_account(session, orders_enabled=True)
            order_id = await repository.create_order(session)
            thread_id = uuid.uuid4()
            await repository.attach_mail(
                session, order_id=order_id, account_id=account_id,
                msg_key="<original2@example.com>", message_id=None, thread_id=thread_id,
                subject="Order NK-2 confirmed", from_addr="shop@example.com",
                received_at=_NOW, attached_by="ai",
            )
            sent_folder = await _seed_folder(session, account_id=account_id, special_use="sent")
            reply_id, _ = await _seed_message(
                session, account_id=account_id, folder_id=sent_folder,
                subject="Re: thanks", thread_id=thread_id,
            )

        event = PostimapEvent(
            v=1, type="message", op="insert", id=str(reply_id),
            account_id=str(account_id), folder_id=str(sent_folder),
        )
        await enqueue_thread_follow_up(migrated_db, event)

        async with migrated_db.session() as session:
            job = (
                await session.execute(
                    select(OrderJob).where(
                        OrderJob.account_id == account_id, OrderJob.message_id == reply_id,
                    )
                )
            ).scalar_one()
        assert job.origin == "thread"
        assert job.kind == "mail"
