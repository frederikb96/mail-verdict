"""
The orders worker reading documents, against a real database and the real
extraction pool: attachment text and codes reach the decide call and the
number search, documents and code facts are stored with the order, the write
call stores a kind, event dates and typed facts (identifiers as a union,
display facts replaced), and a rewrite repairs an order written before
documents existed.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import select, text, update

from mail_verdict.config import get_config
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Order, OrderDocument, OrderFact, OrderMail
from mail_verdict.orders import documents as order_documents
from mail_verdict.orders import repository
from mail_verdict.orders.content import AttachmentRef
from mail_verdict.orders.documents import read_mail_documents, store_mail_documents
from mail_verdict.orders.extract import EXTRACTOR_VERSION
from mail_verdict.orders.extract.model import Barcode, ExtractedDocument
from mail_verdict.orders.worker import _handle_mail_job, _handle_write_job
from mail_verdict.pipeline.contracts import StageUnavailable
from mail_verdict.queue.circuit import CircuitBreaker
from mail_verdict.settings.credentials import ProviderCredentialRepository
from mail_verdict.settings.service import SettingsService
from tests.helpers.documents import make_pdf, png
from tests.helpers.orders_pg import (
    ModelStub,
    seed_attachment,
    ticket_pdf,
)
from tests.pg.test_orders_worker import (
    _NOW,
    _enqueue_mail_job,
    _seed_account,
    _seed_folder,
    _seed_message,
    _settings_service,
)

pytestmark = pytest.mark.asyncio

_STUB_ENDPOINT = {"base_url": "http://models.invalid/v1", "model": "stub-model"}
_NEW_DECISION = {"mail_kind": "ticket", "reason": "r", "target": "new", "identifiers": []}


def _cred_repo(db: DatabaseConnection) -> ProviderCredentialRepository:
    return ProviderCredentialRepository(db, encryption_key="")


async def _stub_tasks(db: DatabaseConnection, *tasks: str) -> SettingsService:
    """Fake providers for both tasks, except the named ones, which go to a
    custom provider the test stubs out (`model_stub`). The breakers of that
    provider start closed: an earlier test of the shared database may have
    tripped them, and the worker consults them before reading anything."""
    async with db.session() as session:
        await session.execute(
            text("DELETE FROM circuit_breakers WHERE name LIKE 'custom:orders.%'")
        )
    service = await _settings_service(db)
    update_values: dict[str, Any] = {}
    for task in tasks:
        update_values.update({
            f"{task}_provider": "custom", f"{task}_base_url": _STUB_ENDPOINT["base_url"],
            f"{task}_model": _STUB_ENDPOINT["model"], f"{task}_reasoning_effort": "none",
        })
    await service.update("orders", update_values)
    return service


async def _mail_with_pdf(
    db: DatabaseConnection, number: str, *, subject: str = "Your tickets", pdf: bytes | None = None,
    body: str = "Tickets attached.", account_id: uuid.UUID | None = None,
    received_at: datetime = _NOW,
) -> tuple[uuid.UUID, uuid.UUID, str, uuid.UUID, dict[str, object]]:
    """A mail with one PDF attachment and its queued job: (account, message,
    msg_key, attachment, job row)."""
    async with db.session() as session:
        if account_id is None:
            account_id = await _seed_account(session, orders_enabled=True)
            folder_id = await _seed_folder(session, account_id=account_id)
        else:
            folder_id = (
                await session.execute(
                    text("SELECT id FROM folders WHERE account_id = :a"), {"a": account_id}
                )
            ).scalar_one()
        message_id, key = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, subject=subject, body_text=body,
            received_at=received_at,
        )
        attachment_id = await seed_attachment(
            session, message_id=message_id, filename="ticket.pdf",
            content_type="application/pdf", data=pdf or ticket_pdf(number),
        )
        row = await _enqueue_mail_job(
            session, account_id=account_id, message_id=message_id, msg_key=key,
        )
    return account_id, message_id, key, attachment_id, row


async def _order_of(db: DatabaseConnection, account_id: uuid.UUID) -> uuid.UUID:
    async with db.session() as session:
        return (
            await session.execute(
                select(OrderMail.order_id).where(OrderMail.account_id == account_id)
            )
        ).scalar_one()


async def _documents(db: DatabaseConnection, order_id: uuid.UUID) -> list[OrderDocument]:
    async with db.session() as session:
        result = await session.execute(
            select(OrderDocument).where(OrderDocument.order_id == order_id)
            .order_by(OrderDocument.filename)
        )
        return list(result.scalars().all())


async def _facts(db: DatabaseConnection, order_id: uuid.UUID) -> list[OrderFact]:
    async with db.session() as session:
        result = await session.execute(
            select(OrderFact).where(OrderFact.order_id == order_id).order_by(OrderFact.position)
        )
        return list(result.scalars().all())


async def _run_write(
    db: DatabaseConnection, service: SettingsService, order_id: uuid.UUID,
) -> None:
    async with db.session() as session:
        await repository.enqueue_write_job(session, order_id, priority=0)
        job_id = (
            await session.execute(
                text(
                    "SELECT id FROM order_jobs WHERE kind = 'write' AND order_id = :o "
                    "AND status = 'pending'"
                ),
                {"o": order_id},
            )
        ).scalar_one()
    await _handle_write_job(
        {"id": job_id, "kind": "write", "order_id": order_id}, db, _cred_repo(db), service,
    )


class TestTheDecideCallReadsAttachments:
    async def test_attachment_text_and_codes_reach_the_decide_prompt(
        self, migrated_db: DatabaseConnection, orders_config: None, model_stub: ModelStub,
        number: str,
    ) -> None:
        service = await _stub_tasks(migrated_db, "decide")
        model_stub.answers["decide"] = _NEW_DECISION
        account_id, message_id, _, _, row = await _mail_with_pdf(migrated_db, number)
        async with migrated_db.session() as session:
            await seed_attachment(
                session, message_id=message_id, filename="logo.png", content_type="image/png",
                data=png(Image.new("L", (20, 20), 255)),
                content_id="logo",
            )

        await _handle_mail_job(row, migrated_db, _cred_repo(migrated_db), service)

        prompt = model_stub.last("decide")["user_prompt"]
        assert 'attachments: "ticket.pdf (application/pdf), logo.png (image/png)"' in prompt
        assert '<attachment n="1" name="ticket.pdf" type="application/pdf" pages=1' in prompt
        assert f"Order number {number}" in prompt
        assert '[code d1:b1 QR \\"5471205382\\"]' in prompt
        # The logo holds nothing to read, so the model is not shown it.
        assert "logo.png\" type" not in prompt

    async def test_the_documents_are_stored_with_the_order_and_every_code_gets_a_fact(
        self, migrated_db: DatabaseConnection, orders_config: None, number: str,
    ) -> None:
        service = await _settings_service(migrated_db)
        account_id, _, key, attachment_id, row = await _mail_with_pdf(migrated_db, number)

        await _handle_mail_job(row, migrated_db, _cred_repo(migrated_db), service)

        order_id = await _order_of(migrated_db, account_id)
        [document] = await _documents(migrated_db, order_id)
        assert (document.source, document.account_id, document.msg_key) == (
            "mail", account_id, key,
        )
        assert (document.attachment_id, document.method, document.page_count) == (
            attachment_id, "text_layer", 1,
        )
        assert document.extractor_version == EXTRACTOR_VERSION
        assert f"Order number {number}" in document.text
        assert [(b["index"], b["format"], b["text"]) for b in document.barcodes] == [
            (1, "QR", "5471205382"),
        ]
        facts = await _facts(migrated_db, order_id)
        code = next(f for f in facts if f.kind == "code")
        assert (code.document_id, code.barcode_index, code.source, code.value) == (
            document.id, 1, "code", "5471205382",
        )
        # The number only the PDF names is an identifier of the order.
        assert ("order_number", number) in [(f.kind, f.value) for f in facts]

    async def test_a_number_only_a_pdf_names_lets_a_later_carrier_mail_join(
        self, migrated_db: DatabaseConnection, orders_config: None, number: str,
    ) -> None:
        service = await _settings_service(migrated_db)
        pdf = make_pdf(texts=[
            (72, 700, "Parcel Sendungsnummer 00340434161094042557"),
            (72, 680, "Shipped with DHL to the address of the person who ordered"),
        ])
        account_id, _, _, _, shop_row = await _mail_with_pdf(
            migrated_db, number, subject="Your order has shipped", pdf=pdf,
            body="Thanks for shopping.",
        )
        await _handle_mail_job(shop_row, migrated_db, _cred_repo(migrated_db), service)
        order_id = await _order_of(migrated_db, account_id)

        async with migrated_db.session() as session:
            folder_id = (
                await session.execute(
                    text("SELECT id FROM folders WHERE account_id = :a"), {"a": account_id}
                )
            ).scalar_one()
            carrier_id, carrier_key = await _seed_message(
                session, account_id=account_id, folder_id=folder_id,
                subject="Delivery today", from_addr="noreply@dhl.example",
                body_text="Your parcel 00340434161094042557 arrives today.",
            )
            carrier_row = await _enqueue_mail_job(
                session, account_id=account_id, message_id=carrier_id, msg_key=carrier_key,
            )
        await _handle_mail_job(carrier_row, migrated_db, _cred_repo(migrated_db), service)

        async with migrated_db.session() as session:
            joined = (
                await session.execute(
                    select(OrderMail.order_id).where(OrderMail.msg_key == carrier_key)
                )
            ).scalar_one()
        assert joined == order_id
        facts = await _facts(migrated_db, order_id)
        assert ("tracking_number", "00340434161094042557") in [(f.kind, f.value) for f in facts]


    async def test_a_number_only_the_new_mails_pdf_names_finds_the_order_too(
        self, migrated_db: DatabaseConnection, orders_config: None, number: str,
    ) -> None:
        service = await _settings_service(migrated_db)
        account_id, _, _, _, shop_row = await _mail_with_pdf(
            migrated_db, number, subject="Your order has shipped",
            body=f"Tracking number: 1Z999AA10123456785. Order {number}.",
        )
        await _handle_mail_job(shop_row, migrated_db, _cred_repo(migrated_db), service)
        order_id = await _order_of(migrated_db, account_id)
        label = make_pdf(texts=[
            (72, 700, "Delivery notice for parcel 1Z999AA10123456785"),
            (72, 680, "It will be delivered to the address of the person who ordered"),
        ])
        _, _, carrier_key, _, carrier_row = await _mail_with_pdf(
            migrated_db, number, subject="Delivery notice", pdf=label, body="See attachment.",
            account_id=account_id,
        )

        await _handle_mail_job(carrier_row, migrated_db, _cred_repo(migrated_db), service)

        async with migrated_db.session() as session:
            joined = (
                await session.execute(
                    select(OrderMail.order_id).where(OrderMail.msg_key == carrier_key)
                )
            ).scalar_one()
        assert joined == order_id


class TestTheWriteCallStoresKindAndFacts:
    async def test_kind_event_dates_and_typed_facts_land_and_codes_are_not_doubled(
        self, migrated_db: DatabaseConnection, orders_config: None, model_stub: ModelStub,
        number: str,
    ) -> None:
        service = await _stub_tasks(migrated_db, "write")
        account_id, _, _, _, row = await _mail_with_pdf(migrated_db, number)
        await _handle_mail_job(row, migrated_db, _cred_repo(migrated_db), service)
        order_id = await _order_of(migrated_db, account_id)

        def answer(kwargs: dict[str, Any]) -> dict[str, Any]:
            prompt = kwargs["user_prompt"]
            assert '<attachment n="1" name="ticket.pdf"' in prompt
            assert "d1:b1" in prompt
            fact = {"carrier": "", "code_ref": ""}
            return {
                "merchant": "Rail Co", "subject": "Heerlen to Amsterdam", "status": "ticket issued",
                "open": True, "kind": "ticket", "event_start": "2026-10-31",
                "event_end": "2026-10-31", "expected_until": "2026-10-31", "icon": "train",
                "summary": "A train ticket.",
                "facts": [
                    {**fact, "kind": "booking_code", "label": "Booking", "value": number},
                    {**fact, "kind": "tracking_number", "label": "Parcel",
                     "value": "1Z999AA10123456784", "carrier": "ups"},
                    {**fact, "kind": "place", "label": "Station", "value": "Heerlen"},
                    {**fact, "kind": "date", "label": "Departure", "value": "2026-10-31"},
                    {**fact, "kind": "code", "label": "Ticketcode", "value": "",
                     "code_ref": "d1:b1"},
                ],
            }

        model_stub.answers["write"] = answer
        await _run_write(migrated_db, service, order_id)

        async with migrated_db.session() as session:
            order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        assert (order.kind, order.event_start, order.event_end) == (
            "ticket", date(2026, 10, 31), date(2026, 10, 31),
        )
        assert order.written_at is not None and order.summary == "A train ticket."

        facts = await _facts(migrated_db, order_id)
        assert [(f.kind, f.label, f.source) for f in facts] == [
            ("booking_code", "Booking", "ai"), ("tracking_number", "Parcel", "ai"),
            ("place", "Station", "ai"), ("date", "Departure", "ai"),
            ("code", "Ticketcode", "ai"),
        ]
        assert [f.position for f in facts] == [0, 1, 2, 3, 4]
        assert facts[1].carrier == "ups"
        assert facts[3].date_value == date(2026, 10, 31)
        assert facts[4].barcode_index == 1 and facts[4].document_id is not None

    async def test_identifiers_accumulate_and_display_facts_are_replaced_by_every_write(
        self, migrated_db: DatabaseConnection, orders_config: None, number: str,
    ) -> None:
        service = await _settings_service(migrated_db)
        account_id, _, _, _, row = await _mail_with_pdf(
            migrated_db, number, subject=f"Order {number} confirmed",
        )
        await _handle_mail_job(row, migrated_db, _cred_repo(migrated_db), service)
        order_id = await _order_of(migrated_db, account_id)
        async with migrated_db.session() as session:
            await repository.upsert_identifier_fact(
                session, order_id, kind="invoice_number", value="RE-2026-0042",
            )
            for source, value in (("ai", "Old station"), ("user", "Typed by hand")):
                await session.execute(
                    text(
                        "INSERT INTO order_facts (order_id, kind, label, value, source) "
                        "VALUES (:o, 'place', 'Place', :v, :s)"
                    ),
                    {"o": order_id, "v": value, "s": source},
                )

        await _run_write(migrated_db, service, order_id)

        facts = [(f.kind, f.value) for f in await _facts(migrated_db, order_id)]
        assert ("invoice_number", "RE-2026-0042") in facts
        assert ("order_number", number) in facts
        assert ("place", "Old station") not in facts
        assert ("place", "Typed by hand") in facts

    async def test_a_kind_a_person_set_survives_a_rewrite(
        self, migrated_db: DatabaseConnection, orders_config: None, number: str,
    ) -> None:
        service = await _settings_service(migrated_db)
        account_id, _, _, _, row = await _mail_with_pdf(migrated_db, number, subject="Your ticket")
        await _handle_mail_job(row, migrated_db, _cred_repo(migrated_db), service)
        order_id = await _order_of(migrated_db, account_id)
        async with migrated_db.session() as session:
            await repository.update_controls(session, order_id, kind="other")

        await _run_write(migrated_db, service, order_id)

        async with migrated_db.session() as session:
            order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        assert (order.kind, order.kind_set_by) == ("other", "user")


class TestARewriteRepairsAnOldOrder:
    async def _old_order(
        self, db: DatabaseConnection, number: str,
    ) -> tuple[uuid.UUID, uuid.UUID, str, uuid.UUID]:
        """An order written before documents existed: its mail is attached,
        its attachment is in PostIMAP's table, nothing else."""
        async with db.session() as session:
            account_id = await _seed_account(session, orders_enabled=True)
            folder_id = await _seed_folder(session, account_id=account_id)
            message_id, key = await _seed_message(
                session, account_id=account_id, folder_id=folder_id, subject=f"Ticket {number}",
            )
            attachment_id = await seed_attachment(
                session, message_id=message_id, filename="ticket.pdf",
                content_type="application/pdf", data=ticket_pdf(number),
            )
            order_id = await repository.create_order(session)
            await repository.attach_mail(
                session, order_id=order_id, account_id=account_id, msg_key=key,
                message_id=message_id, thread_id=None, subject=f"Ticket {number}",
                from_addr="shop@example.com", received_at=_NOW, attached_by="ai",
            )
            await repository.recompute_aggregates(session, order_id)
            await repository.write_order_text(
                session, order_id, merchant="Shop", subject="Ticket", status="issued",
                is_open=True, icon="ticket", summary="Old text.", model="old",
            )
        return account_id, order_id, key, attachment_id

    async def test_a_rewrite_reads_the_documents_the_order_never_had(
        self, migrated_db: DatabaseConnection, orders_config: None, number: str,
    ) -> None:
        service = await _settings_service(migrated_db)
        _, order_id, _, attachment_id = await self._old_order(migrated_db, number)
        assert await _documents(migrated_db, order_id) == []

        await _run_write(migrated_db, service, order_id)

        [document] = await _documents(migrated_db, order_id)
        assert document.attachment_id == attachment_id and number in document.text
        facts = await _facts(migrated_db, order_id)
        assert [f.kind for f in facts if f.kind == "code"] == ["code"]

    async def test_a_document_read_by_an_older_extractor_is_read_again(
        self, migrated_db: DatabaseConnection, orders_config: None, number: str,
    ) -> None:
        service = await _settings_service(migrated_db)
        _, order_id, _, _ = await self._old_order(migrated_db, number)
        await _run_write(migrated_db, service, order_id)
        [first] = await _documents(migrated_db, order_id)
        async with migrated_db.session() as session:
            await session.execute(
                update(OrderDocument).where(OrderDocument.id == first.id)
                .values(extractor_version=EXTRACTOR_VERSION - 1, text="read badly")
            )

        await _run_write(migrated_db, service, order_id)

        [again] = await _documents(migrated_db, order_id)
        assert again.id == first.id and again.extractor_version == EXTRACTOR_VERSION
        assert number in again.text

    async def test_attachments_renumbered_by_a_resync_keep_their_document(
        self, migrated_db: DatabaseConnection, orders_config: None, number: str,
    ) -> None:
        service = await _settings_service(migrated_db)
        account_id, order_id, _, old_attachment = await self._old_order(migrated_db, number)
        await _run_write(migrated_db, service, order_id)
        [first] = await _documents(migrated_db, order_id)
        new_attachment = uuid.uuid4()
        async with migrated_db.session() as session:
            await session.execute(
                text("UPDATE attachments SET id = :new WHERE id = :old"),
                {"new": new_attachment, "old": old_attachment},
            )
            # Proves the document is re-pointed, not read again.
            await session.execute(
                update(OrderDocument).where(OrderDocument.id == first.id)
                .values(text="kept as it was")
            )

        await _run_write(migrated_db, service, order_id)

        [after] = await _documents(migrated_db, order_id)
        assert (after.id, after.attachment_id, after.text) == (
            first.id, new_attachment, "kept as it was",
        )

    async def test_a_mail_that_is_gone_keeps_its_documents_in_the_prompt(
        self, migrated_db: DatabaseConnection, orders_config: None, model_stub: ModelStub,
        number: str,
    ) -> None:
        service = await _stub_tasks(migrated_db, "write")
        account_id, order_id, key, _ = await self._old_order(migrated_db, number)
        model_stub.answers["write"] = {
            "merchant": "Shop", "subject": "Ticket", "status": "issued", "open": True,
            "kind": "ticket", "event_start": "", "event_end": "", "expected_until": "",
            "icon": "ticket", "summary": "s", "facts": [],
        }
        await _run_write(migrated_db, service, order_id)
        async with migrated_db.session() as session:
            await session.execute(
                text("UPDATE messages SET expunged_at = now() WHERE account_id = :a"),
                {"a": account_id},
            )

        await _run_write(migrated_db, service, order_id)

        prompt = model_stub.last("write")["user_prompt"]
        assert f"Order number {number}" in prompt
        assert [d.msg_key for d in await _documents(migrated_db, order_id)] == [key]

    async def test_a_mail_that_moved_into_the_glacier_still_gives_its_body_and_documents(
        self, migrated_db: DatabaseConnection, orders_config: None, model_stub: ModelStub,
        number: str,
    ) -> None:
        service = await _stub_tasks(migrated_db, "write")
        account_id, order_id, key, _ = await self._old_order(migrated_db, number)
        model_stub.answers["write"] = {
            "merchant": "Shop", "subject": "Ticket", "status": "issued", "open": True,
            "kind": "ticket", "event_start": "", "event_end": "", "expected_until": "",
            "icon": "ticket", "summary": "s", "facts": [],
        }
        glacier_attachment = uuid.uuid4()
        data = ticket_pdf(number)
        async with migrated_db.session() as session:
            live = (
                await session.execute(
                    text("SELECT id, folder_id, thread_id FROM messages WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).one()
            glacier_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO glacier_messages "
                    "(id, account_id, folder_id, thread_id, message_id, subject, from_addr, "
                    " body_text, raw_source, size_bytes, received_at, is_seen, msg_key, state, "
                    " visible_at, glaciered_at) "
                    "VALUES (:id, :a, :folder, :thread, :key, 'Ticket', 'shop@example.com', "
                    " 'Thanks for your order. See you soon.', :raw, 10, :received, false, :key, "
                    " 'glaciered', now(), now())"
                ),
                {
                    "id": glacier_id, "a": account_id, "folder": uuid.uuid4(),
                    "thread": live.thread_id, "key": key, "raw": b"From: a@b\r\n\r\nx",
                    "received": _NOW,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO glacier_attachments "
                    "(id, glacier_message_id, filename, content_type, size_bytes, data) "
                    "VALUES (:id, :g, 'ticket.pdf', 'application/pdf', :size, :data)"
                ),
                {"id": glacier_attachment, "g": glacier_id, "size": len(data), "data": data},
            )
            await session.execute(
                text("DELETE FROM attachments WHERE message_id = :m"), {"m": live.id}
            )
            await session.execute(
                text("UPDATE messages SET expunged_at = now() WHERE id = :m"), {"m": live.id}
            )

        await _run_write(migrated_db, service, order_id)

        prompt = model_stub.last("write")["user_prompt"]
        assert "Thanks for your order" in prompt
        assert f"Order number {number}" in prompt
        [document] = await _documents(migrated_db, order_id)
        assert (document.attachment_id, document.method) == (glacier_attachment, "text_layer")
        facts = await _facts(migrated_db, order_id)
        assert [f.value for f in facts if f.kind == "code"] == ["5471205382"]

    async def test_a_failed_read_is_repeated_by_the_next_rewrite_but_an_oversize_one_is_not(
        self, migrated_db: DatabaseConnection, orders_config: None, number: str,
    ) -> None:
        service = await _settings_service(migrated_db)
        _, order_id, _, _ = await self._old_order(migrated_db, number)
        await _run_write(migrated_db, service, order_id)
        [first] = await _documents(migrated_db, order_id)
        async with migrated_db.session() as session:
            await session.execute(
                update(OrderDocument).where(OrderDocument.id == first.id)
                .values(method="failed", text="", barcodes=[], text_chars_total=0)
            )

        await _run_write(migrated_db, service, order_id)

        [again] = await _documents(migrated_db, order_id)
        assert (again.method, number in again.text) == ("text_layer", True)

        limit = get_config().extraction.max_document_bytes
        async with migrated_db.session() as session:
            await session.execute(
                update(OrderDocument).where(OrderDocument.id == first.id)
                .values(method="failed", text="", size_bytes=limit + 1)
            )

        await _run_write(migrated_db, service, order_id)

        [oversize] = await _documents(migrated_db, order_id)
        assert (oversize.method, oversize.text) == ("failed", "")

    async def test_a_failed_file_is_read_again_by_the_next_rewrite(
        self, migrated_db: DatabaseConnection, orders_config: None, number: str,
    ) -> None:
        service = await _settings_service(migrated_db)
        async with migrated_db.session() as session:
            order_id = await repository.create_order(session)
            file_id = (await session.execute(
                text(
                    "INSERT INTO order_files (order_id, filename, content_type, size_bytes, "
                    "sha256, data, status) VALUES (:o, 'ticket.pdf', 'application/pdf', 1, :h, "
                    ":d, 'attached') RETURNING id"
                ),
                {"o": order_id, "h": number, "d": ticket_pdf(number)},
            )).scalar_one()
            await session.execute(
                text(
                    "INSERT INTO order_documents (order_id, source, file_id, filename, "
                    "content_type, size_bytes, method, extractor_version) VALUES "
                    "(:o, 'file', :f, 'ticket.pdf', 'application/pdf', 1000, 'failed', :v)"
                ),
                {"o": order_id, "f": file_id, "v": EXTRACTOR_VERSION},
            )
            await repository.recompute_aggregates(session, order_id)

        await _run_write(migrated_db, service, order_id)

        [document] = await _documents(migrated_db, order_id)
        assert (document.method, number in document.text) == ("text_layer", True)


class TestReadingLimits:
    async def test_an_attachment_over_the_size_limit_is_recorded_failed_unread(
        self, migrated_db: DatabaseConnection, orders_config: None, number: str,
    ) -> None:
        ref = AttachmentRef(
            id=uuid.uuid4(), filename="huge.pdf", content_type="application/pdf",
            size_bytes=10**9, is_inline=False,
        )
        async with migrated_db.session() as session:
            [read] = await read_mail_documents(session, [ref])
            order_id = await repository.create_order(session)
            await store_mail_documents(
                session, order_id=order_id, account_id=uuid.uuid4(), msg_key="k", reads=[read],
            )
        [document] = await _documents(migrated_db, order_id)
        assert (document.method, document.text, document.barcodes) == ("failed", "", [])
        assert document.sha256 is None


class TestReadingOrder:
    async def test_inline_images_never_crowd_a_ticket_out_of_the_attachments_that_are_read(
        self, migrated_db: DatabaseConnection, orders_config: None, number: str,
    ) -> None:
        service = await _settings_service(migrated_db)
        account_id, message_id, _, _, row = await _mail_with_pdf(migrated_db, number)
        async with migrated_db.session() as session:
            for i in range(order_documents._MAX_ATTACHMENTS_READ + 1):
                await seed_attachment(
                    session, message_id=message_id, filename=f"a{i:02d}.png",
                    content_type="image/png", data=png(Image.new("L", (20, 20), 255)),
                    content_id=f"logo{i}",
                )

        await _handle_mail_job(row, migrated_db, _cred_repo(migrated_db), service)

        order_id = await _order_of(migrated_db, account_id)
        by_name = {d.filename: d for d in await _documents(migrated_db, order_id)}
        assert number in by_name["ticket.pdf"].text
        assert len(by_name) == order_documents._MAX_ATTACHMENTS_READ


class TestNothingIsReadWhileTheProviderIsSuspended:
    async def test_a_suspended_decide_provider_releases_the_job_before_any_attachment_is_read(
        self, migrated_db: DatabaseConnection, orders_config: None, model_stub: ModelStub,
        monkeypatch: pytest.MonkeyPatch, number: str,
    ) -> None:
        service = await _stub_tasks(migrated_db, "decide")
        model_stub.answers["decide"] = _NEW_DECISION
        account_id, _, _, _, row = await _mail_with_pdf(migrated_db, number)
        breaker = CircuitBreaker(migrated_db, "custom:orders.decide")
        await breaker.record_unavailable(
            reason="credit balance exhausted", probe_interval=timedelta(minutes=5),
        )

        def no_reading() -> None:
            raise AssertionError("an attachment was read")

        try:
            with monkeypatch.context() as patched:
                patched.setattr(order_documents, "extraction_pool", no_reading)
                with pytest.raises(StageUnavailable):
                    await _handle_mail_job(row, migrated_db, _cred_repo(migrated_db), service)
            assert model_stub.calls == []

            await breaker.record_success()
            await _handle_mail_job(row, migrated_db, _cred_repo(migrated_db), service)
        finally:
            await breaker.record_success()
        assert [c["task"] for c in model_stub.calls] == ["decide"]
        assert await _order_of(migrated_db, account_id) is not None


class TestStoringWhatWasRead:
    async def test_a_lone_surrogate_in_a_pass_text_does_not_fail_the_insert(
        self, migrated_db: DatabaseConnection, orders_config: None,
    ) -> None:
        document = ExtractedDocument(
            filename="pass\ud800.pkpass", content_type="application/vnd.apple.pkpass",
            size_bytes=10, sha256="x", method="pkpass", text="Gate \ud800 12\x00",
            text_chars_total=9,
            barcodes=(Barcode(1, "QR", b"x", "A\ud800B", None, None, None),),
        )
        async with migrated_db.session() as session:
            order_id = await repository.create_order(session)
            await store_mail_documents(
                session, order_id=order_id, account_id=uuid.uuid4(), msg_key="k",
                reads=[order_documents.ReadDocument(uuid.uuid4(), None, document)],
            )
        [stored] = await _documents(migrated_db, order_id)
        assert stored.text == "Gate ? 12"
        assert [b["text"] for b in stored.barcodes] == ["A?B"]
