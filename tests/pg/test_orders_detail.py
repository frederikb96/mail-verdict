"""
The order detail view (api/orders.py's _load_detail) against a real
database: a permanently deleted mail still lists in its order, marked
gone, while the order's other mail stays fully resolvable; and a mail
carrying both an inline image and a PDF attachment lists the PDF as a
document while the inline image never appears.
"""

from __future__ import annotations

import json
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


async def test_documents_come_from_order_documents_and_link_to_the_attachment_where_it_is_now(
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

        pdf_id = uuid.uuid4()
        for attachment_id, filename, content_type, content_id in (
            (pdf_id, "ticket.pdf", "application/pdf", None),
            (uuid.uuid4(), "logo.png", "image/png", "inline-logo-cid"),
        ):
            await session.execute(
                text(
                    "INSERT INTO attachments (id, message_id, filename, content_type, content_id) "
                    "VALUES (:id, :message_id, :filename, :content_type, :content_id)"
                ),
                {
                    "id": attachment_id, "message_id": mail_id, "filename": filename,
                    "content_type": content_type, "content_id": content_id,
                },
            )
        # Only the PDF was extracted; its stored attachment id is stale, so
        # the link has to find the attachment by name and type.
        await session.execute(
            text(
                "INSERT INTO order_documents (order_id, source, account_id, msg_key, "
                "attachment_id, filename, content_type, method, page_count, barcodes) "
                "VALUES (:order_id, 'mail', :account_id, :msg_key, :stale, 'ticket.pdf', "
                "'application/pdf', 'text_layer', 2, CAST(:barcodes AS jsonb))"
            ),
            {
                "order_id": order_id, "account_id": account_id, "msg_key": msg_key,
                "stale": uuid.uuid4(), "barcodes": json.dumps([{"index": 1}, {"index": 2}]),
            },
        )

    async with migrated_db.session() as session:
        detail = await _load_detail(session, order_id)

    assert detail is not None
    assert [d.filename for d in detail.documents] == ["ticket.pdf"]
    document = detail.documents[0]
    assert (document.source, document.method, document.page_count, document.barcode_count) == (
        "mail", "text_layer", 2, 2,
    )
    assert document.message_id == mail_id
    assert document.download_url == f"/api/messages/{mail_id}/attachments/{pdf_id}"
    assert document.received_at == _NOW


async def test_a_file_document_links_to_the_file_download_route(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        order_id = await repository.create_order(session)
        file_id = (
            await session.execute(
                text(
                    "INSERT INTO order_files "
                    "(order_id, filename, size_bytes, sha256, data, status) "
                    "VALUES (:order_id, 'scan.pdf', 3, 'abc', '\\x010203', 'attached') "
                    "RETURNING id"
                ),
                {"order_id": order_id},
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO order_documents (order_id, source, file_id, filename, method) "
                "VALUES (:order_id, 'file', :file_id, 'scan.pdf', 'ocr')"
            ),
            {"order_id": order_id, "file_id": file_id},
        )
        await repository.recompute_aggregates(session, order_id)
        await repository.write_order_text(
            session, order_id, merchant="Shop", subject="Scan", status="open",
            is_open=True, icon="receipt", summary="s", model="fake",
        )

    async with migrated_db.session() as session:
        detail = await _load_detail(session, order_id)

    assert detail is not None
    assert detail.file_count == 1 and detail.mail_count == 0
    assert detail.first_activity_at is not None
    assert [d.download_url for d in detail.documents] == [f"/api/orders/{order_id}/files/{file_id}"]
    assert detail.documents[0].source == "file"


async def test_facts_come_out_in_order_with_tracking_links_and_codes(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        order_id = await repository.create_order(session)
        await repository.store_identifier_facts(
            session, order_id, [("order_number", "NK-48213")],
        )
        document_id = (
            await session.execute(
                text(
                    "INSERT INTO order_documents "
                    "(order_id, source, filename, method, barcodes) VALUES "
                    "(:order_id, 'file', 'ticket.pdf', 'text_layer', CAST(:barcodes AS jsonb)) "
                    "RETURNING id"
                ),
                {
                    "order_id": order_id,
                    "barcodes": json.dumps([
                        {"index": 1, "format": "Aztec", "text": "5471205382", "page": 1},
                        {"index": 2, "format": "QR", "text": "\x01\x02 binary", "page": 2},
                    ]),
                },
            )
        ).scalar_one()
        for position, (kind, label, value, carrier, barcode_index) in enumerate((
            ("tracking_number", "DHL", "00340 434 161 094 042 2", "dhl", None),
            ("tracking_number", "Mystery", "ZX12", "amazon", None),
            ("place", "Station", "Aachen Hbf", None, None),
            ("code", "Ticketcode", "", None, 1),
            ("code", "Return", "", None, 2),
        ), start=10):
            await session.execute(
                text(
                    "INSERT INTO order_facts (order_id, kind, label, value, carrier, "
                    "document_id, barcode_index, position) VALUES (:order_id, :kind, :label, "
                    ":value, :carrier, :document_id, :barcode_index, :position)"
                ),
                {
                    "order_id": order_id, "kind": kind, "label": label, "value": value,
                    "carrier": carrier, "position": position, "barcode_index": barcode_index,
                    "document_id": document_id if kind == "code" else None,
                },
            )
        await repository.recompute_aggregates(session, order_id)
        await repository.write_order_text(
            session, order_id, merchant="Shop", subject="Order", status="open",
            is_open=True, icon="package", summary="s", model="fake",
        )

    async with migrated_db.session() as session:
        detail = await _load_detail(session, order_id)

    assert detail is not None
    assert [(f.kind, f.label) for f in detail.facts] == [
        ("order_number", "Order number"), ("tracking_number", "DHL"),
        ("tracking_number", "Mystery"), ("place", "Station"),
        ("code", "Ticketcode"), ("code", "Return"),
    ]
    by_label = {f.label: f for f in detail.facts}
    assert by_label["DHL"].tracking_url == (
        "https://www.dhl.de/de/privatkunden/pakete-empfangen/verfolgen.html"
        "?piececode=00340%20434%20161%20094%20042%202"
    )
    assert by_label["Mystery"].tracking_url is None
    assert by_label["Station"].tracking_url is None and by_label["Station"].code is None
    ticket_code = by_label["Ticketcode"].code
    assert ticket_code is not None
    assert (ticket_code.format, ticket_code.payload_text, ticket_code.page) == (
        "Aztec", "5471205382", 1,
    )
    assert ticket_code.image_url == f"/api/orders/{order_id}/codes/{by_label['Ticketcode'].id}.png"
    return_code = by_label["Return"].code
    assert return_code is not None and return_code.payload_text is None
