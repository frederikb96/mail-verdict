"""
The order detail view (api/orders.py's _load_detail) against a real
database: a permanently deleted mail still lists in its order, marked
gone, while the order's other mail stays fully resolvable; and a mail
carrying both an inline image and a PDF attachment lists the PDF as a
document while the inline image never appears.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.orders import _load_detail
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.orders import repository

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


async def _seed_account_and_folder(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    account_id = uuid.uuid4()
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text("INSERT INTO folders (id, account_id, imap_name) VALUES (:id, :account_id, 'INBOX')"),
        {"id": folder_id, "account_id": account_id},
    )
    return account_id, folder_id


async def _seed_message(
    session: AsyncSession, *, account_id: uuid.UUID, folder_id: uuid.UUID,
    subject: str, expunged: bool = False,
) -> tuple[uuid.UUID, str]:
    mail_id = uuid.uuid4()
    header = f"<{uuid.uuid4()}@example.com>"
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, from_addr, "
            "subject, body_text, received_at, size_bytes, is_seen, expunged_at) "
            "VALUES (:id, :account_id, :folder_id, :uid, :thread_id, :message_id, :from_addr, "
            ":subject, 'body', :received_at, 512, false, :expunged_at)"
        ),
        {
            "id": mail_id, "account_id": account_id, "folder_id": folder_id,
            "uid": abs(hash(mail_id)) % 100000 + 1, "thread_id": uuid.uuid4(),
            "message_id": header, "from_addr": "shop@example.com", "subject": subject,
            "received_at": _NOW, "expunged_at": _NOW if expunged else None,
        },
    )
    return mail_id, header


async def test_a_permanently_deleted_mail_stays_listed_marked_gone_others_stay_open(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        order_id = await repository.create_order(session)

        gone_id, gone_key = await _seed_message(
            session, account_id=account_id, folder_id=folder_id,
            subject="Order gone", expunged=True,
        )
        live_id, live_key = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, subject="Order still here",
        )
        await repository.attach_mail(
            session, order_id=order_id, account_id=account_id, msg_key=gone_key,
            message_id=gone_id, thread_id=None, subject="Order gone",
            from_addr="shop@example.com", received_at=_NOW, attached_by="ai",
        )
        await repository.attach_mail(
            session, order_id=order_id, account_id=account_id, msg_key=live_key,
            message_id=live_id, thread_id=None, subject="Order still here",
            from_addr="shop@example.com", received_at=_NOW, attached_by="ai",
        )
        await repository.recompute_aggregates(session, order_id)
        await repository.write_order_text(
            session, order_id, merchant="Shop", subject="Order", status="open",
            is_open=True, icon="package", summary="s", model="fake",
        )

    async with migrated_db.session() as session:
        detail = await _load_detail(session, order_id)

    assert detail is not None
    by_key = {m.message_id: m for m in detail.mails if m.message_id is not None}
    gone = next(m for m in detail.mails if m.subject == "Order gone")
    live = next(m for m in detail.mails if m.subject == "Order still here")
    assert gone.location == "gone"
    assert gone.message_id is None
    assert live.location == "mailbox"
    assert live.message_id == live_id
    assert live_id in by_key
    assert len(detail.mails) == 2


async def test_a_pdf_attachment_lists_as_a_document_an_inline_image_never_does(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        order_id = await repository.create_order(session)
        mail_id, msg_key = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, subject="Ticket with attachments",
        )
        await repository.attach_mail(
            session, order_id=order_id, account_id=account_id, msg_key=msg_key,
            message_id=mail_id, thread_id=None, subject="Ticket with attachments",
            from_addr="shop@example.com", received_at=_NOW, attached_by="ai",
        )
        await repository.recompute_aggregates(session, order_id)
        await repository.write_order_text(
            session, order_id, merchant="Shop", subject="Order", status="open",
            is_open=True, icon="receipt", summary="s", model="fake",
        )

        await session.execute(
            text(
                "INSERT INTO attachments (id, message_id, filename, content_type, content_id) "
                "VALUES (:id, :message_id, 'ticket.pdf', 'application/pdf', NULL)"
            ),
            {"id": uuid.uuid4(), "message_id": mail_id},
        )
        await session.execute(
            text(
                "INSERT INTO attachments (id, message_id, filename, content_type, content_id) "
                "VALUES (:id, :message_id, 'logo.png', 'image/png', 'inline-logo-cid')"
            ),
            {"id": uuid.uuid4(), "message_id": mail_id},
        )

    async with migrated_db.session() as session:
        detail = await _load_detail(session, order_id)

    assert detail is not None
    assert [d.filename for d in detail.documents] == ["ticket.pdf"]
    assert detail.documents[0].content_type == "application/pdf"
