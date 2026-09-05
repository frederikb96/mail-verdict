"""
outbox/pending.py's staging table against a real database: staging a send,
cancelling it before its window passes, and the periodic worker moving a
due, uncancelled row into a real outbox row with its attachments.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Outbox, OutboxAttachment, PendingSend
from mail_verdict.outbox.pending import (
    _process_due_sends,
    cancel_pending_send,
    list_pending_sends,
    stage_send,
)


async def _seed_account(session: AsyncSession) -> uuid.UUID:
    from sqlalchemy import text

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


async def _stage(
    migrated_db: DatabaseConnection, account_id: uuid.UUID, undo_seconds: float,
) -> uuid.UUID:
    async with migrated_db.session() as session:
        row = await stage_send(
            session,
            account_id=account_id,
            from_addr="me@example.com",
            to_addrs=["them@example.com"],
            cc_addrs=None,
            bcc_addrs=None,
            subject="hi",
            body_text="hi",
            body_html=None,
            in_reply_to=None,
            references=None,
            replaces_message_id=None,
            attachments=[],
            undo_seconds=undo_seconds,
        )
        await session.commit()
        return row.id


class TestStageSend:
    @pytest.mark.asyncio
    async def test_a_staged_send_is_not_yet_in_outbox(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            await session.commit()

        pending_id = await _stage(migrated_db, account_id, undo_seconds=30)

        async with migrated_db.session() as session:
            outbox_row = await session.scalar(select(Outbox).where(Outbox.id == pending_id))
            pending = await session.scalar(
                select(PendingSend).where(PendingSend.id == pending_id)
            )
        assert outbox_row is None
        assert pending is not None
        assert pending.cancelled_at is None

    @pytest.mark.asyncio
    async def test_list_pending_sends_excludes_a_cancelled_row(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            await session.commit()

        pending_id = await _stage(migrated_db, account_id, undo_seconds=30)

        async with migrated_db.session() as session:
            cancelled = await cancel_pending_send(session, pending_id)
            await session.commit()
        assert cancelled is True

        async with migrated_db.session() as session:
            rows = await list_pending_sends(session, account_id)
        assert rows == []

    @pytest.mark.asyncio
    async def test_cancelling_twice_the_second_time_reports_it_was_too_late(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            await session.commit()

        pending_id = await _stage(migrated_db, account_id, undo_seconds=30)

        async with migrated_db.session() as session:
            first = await cancel_pending_send(session, pending_id)
            await session.commit()
        async with migrated_db.session() as session:
            second = await cancel_pending_send(session, pending_id)
            await session.commit()

        assert first is True
        assert second is False


class TestProcessDueSends:
    @pytest.mark.asyncio
    async def test_a_due_uncancelled_send_becomes_a_real_outbox_row(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            await session.commit()

        # Already in the past, so the worker's next tick claims it
        # immediately rather than racing real clock time.
        pending_id = await _stage(migrated_db, account_id, undo_seconds=-5)

        await _process_due_sends(migrated_db)

        async with migrated_db.session() as session:
            pending = await session.scalar(
                select(PendingSend).where(PendingSend.id == pending_id)
            )
            outbox_row = await session.scalar(
                select(Outbox).where(Outbox.account_id == account_id)
            )
        assert pending is None
        assert outbox_row is not None
        assert outbox_row.subject == "hi"
        assert outbox_row.status == "pending"

    @pytest.mark.asyncio
    async def test_attachments_move_along_with_the_send_content_id_included(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            await session.commit()

        async with migrated_db.session() as session:
            await stage_send(
                session,
                account_id=account_id,
                from_addr="me@example.com",
                to_addrs=["them@example.com"],
                cc_addrs=None,
                bcc_addrs=None,
                subject="pic",
                body_text="see attached",
                body_html='<img src="cid:img1">',
                in_reply_to=None,
                references=None,
                replaces_message_id=None,
                attachments=[("pic.png", "image/png", b"\x89PNG", "img1")],
                undo_seconds=-5,
            )
            await session.commit()

        await _process_due_sends(migrated_db)

        async with migrated_db.session() as session:
            outbox_row = await session.scalar(
                select(Outbox).where(Outbox.account_id == account_id)
            )
            assert outbox_row is not None
            attachment = await session.scalar(
                select(OutboxAttachment).where(OutboxAttachment.outbox_id == outbox_row.id)
            )

        assert attachment is not None
        assert attachment.content_id == "img1"
        assert attachment.filename == "pic.png"

    @pytest.mark.asyncio
    async def test_a_cancelled_send_is_never_processed(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            await session.commit()

        pending_id = await _stage(migrated_db, account_id, undo_seconds=-5)
        async with migrated_db.session() as session:
            await cancel_pending_send(session, pending_id)
            await session.commit()

        await _process_due_sends(migrated_db)

        async with migrated_db.session() as session:
            outbox_row = await session.scalar(
                select(Outbox).where(Outbox.account_id == account_id)
            )
            pending = await session.scalar(
                select(PendingSend).where(PendingSend.id == pending_id)
            )
        assert outbox_row is None
        assert pending is not None
