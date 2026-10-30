"""
Files added to the orders register by hand, against a real database and the
real extraction pool: the upload routes and their limits, the file job
placing a file in an existing entry, a chosen entry or a new one (never
"none"), an entry made only of files working like any other, deletion, and
the pruning of files that could not be placed.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
import zxingcpp
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select, text

from mail_verdict.api.orders import router
from mail_verdict.config import get_config
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Order, OrderDocument, OrderFile, OrderJob
from mail_verdict.orders import files, repository, worker
from mail_verdict.orders.auto_close import close_overdue_orders
from mail_verdict.orders.candidates import find_candidates
from mail_verdict.orders.lookup import OrderLookup
from mail_verdict.orders.worker import _fail, _handle_file_job, _handle_item, _handle_mail_job
from mail_verdict.pipeline.contracts import StageRefused, StageTransient
from tests.helpers.orders_pg import ModelStub, ticket_pdf
from tests.pg.test_orders_documents import (
    _cred_repo,
    _documents,
    _facts,
    _mail_with_pdf,
    _order_of,
    _run_write,
    _stub_tasks,
)
from tests.pg.test_orders_worker import _settings_service

_ACCOUNT_FREE = uuid.UUID(int=0)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """One persistent portal for the whole test: the shared database's
    connections are bound to the loop that opened them."""
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def _upload(
    client: TestClient, db: DatabaseConnection, data: bytes, name: str = "ticket.pdf",
    order_id: uuid.UUID | None = None, content_type: str = "application/pdf",
) -> Any:
    with patch("mail_verdict.api.orders.get_db_connection", return_value=db):
        return client.post(
            "/orders/files", files={"file": (name, data, content_type)},
            data={"order_id": str(order_id)} if order_id else {},
        )


async def _file_job_row(db: DatabaseConnection, file_id: uuid.UUID) -> dict[str, Any]:
    async with db.session() as session:
        job = (
            await session.execute(
                select(OrderJob).where(OrderJob.kind == "file", OrderJob.file_id == file_id)
            )
        ).scalar_one()
    return {"id": job.id, "kind": "file", "file_id": file_id, "origin": job.origin}


async def _place(
    db: DatabaseConnection, file_id: uuid.UUID, *, write: bool = True,
) -> uuid.UUID:
    """Run the file job (and the write job it queues) with the fake providers."""
    service = await _settings_service(db)
    row = await _file_job_row(db, file_id)
    await _handle_file_job(row, db, _cred_repo(db), service)
    async with db.session() as session:
        order_id = (
            await session.execute(select(OrderFile.order_id).where(OrderFile.id == file_id))
        ).scalar_one()
    if write:
        await _run_write(db, service, order_id)
    return order_id


async def _place_unwritten(db: DatabaseConnection, file_id: uuid.UUID) -> uuid.UUID:
    return await _place(db, file_id, write=False)


def _file_status(client: TestClient, db: DatabaseConnection, file_id: uuid.UUID) -> Any:
    with patch("mail_verdict.api.orders.get_db_connection", return_value=db):
        return client.get(f"/orders/files/{file_id}")


def _get(client: TestClient, db: DatabaseConnection, path: str) -> Any:
    with patch("mail_verdict.api.orders.get_db_connection", return_value=db):
        return client.get(path)


class TestAnUploadedFileBecomesAnEntry:
    def test_a_ticket_pdf_gets_its_own_entry_with_its_document_codes_and_download(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        number: str,
    ) -> None:
        pdf = ticket_pdf(number)
        response = _upload(client, migrated_db, pdf)
        assert response.status_code == 202, response.text
        body = response.json()
        assert (body["status"], body["order_id"], body["last_error"]) == ("pending", None, None)
        file_id = uuid.UUID(body["file_id"])

        order_id = client.portal.call(_place, migrated_db, file_id)

        status = _file_status(client, migrated_db, file_id).json()
        assert (status["status"], status["order_id"]) == ("attached", str(order_id))
        detail = _get(client, migrated_db, f"/orders/{order_id}").json()
        assert (detail["mail_count"], detail["file_count"]) == (0, 1)
        assert detail["text_stale"] is False
        [document] = detail["documents"]
        assert document["source"] == "file" and document["filename"] == "ticket.pdf"
        assert document["download_url"] == f"/api/orders/{order_id}/files/{file_id}"
        code = next(f for f in detail["facts"] if f["kind"] == "code")
        assert code["code"]["payload_text"] == "5471205382"
        assert number in [f["value"] for f in detail["facts"]]

        download = _get(client, migrated_db, f"/orders/{order_id}/files/{file_id}")
        assert download.status_code == 200 and download.content == pdf
        assert download.headers["content-type"] == "application/pdf"
        assert "ticket.pdf" in download.headers["content-disposition"]

        listing = _get(client, migrated_db, "/orders?limit=500").json()
        assert str(order_id) in [item["id"] for item in listing["items"]]

    def test_a_file_added_to_a_chosen_entry_skips_the_decide_call(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        model_stub: ModelStub, number: str,
    ) -> None:
        async def entry() -> uuid.UUID:
            async with migrated_db.session() as session:
                order_id = await repository.create_order(session)
                await repository.write_order_text(
                    session, order_id, merchant="Rail", subject="Trip", status="ok",
                    is_open=True, icon="train", summary="s", model="fake",
                )
                await session.execute(
                    text("UPDATE orders SET last_activity_at = now() WHERE id = :o"),
                    {"o": order_id},
                )
            return order_id

        chosen = client.portal.call(entry)
        response = _upload(client, migrated_db, ticket_pdf(number), order_id=chosen)
        assert response.status_code == 202 and response.json()["order_id"] == str(chosen)
        file_id = uuid.UUID(response.json()["file_id"])

        async def place() -> None:
            service = await _stub_tasks(migrated_db, "decide")
            await _handle_file_job(
                await _file_job_row(migrated_db, file_id), migrated_db,
                _cred_repo(migrated_db), service,
            )

        client.portal.call(place)

        assert model_stub.calls == []
        assert _file_status(client, migrated_db, file_id).json()["order_id"] == str(chosen)

    def test_the_decide_call_for_a_file_never_offers_none_and_can_join_an_entry(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        model_stub: ModelStub, number: str,
    ) -> None:
        async def open_entry() -> uuid.UUID:
            account_id, _, _, _, row = await _mail_with_pdf(
                migrated_db, number, subject="Booking confirmed",
            )
            await _handle_mail_job(
                row, migrated_db, _cred_repo(migrated_db), await _settings_service(migrated_db),
            )
            return await _order_of(migrated_db, account_id)

        existing = client.portal.call(open_entry)
        file_id = uuid.UUID(_upload(client, migrated_db, ticket_pdf(number)).json()["file_id"])
        model_stub.answers["decide"] = lambda kw: {
            "mail_kind": "ticket", "reason": "same booking", "target": "C1", "identifiers": [],
        }

        async def place() -> None:
            service = await _stub_tasks(migrated_db, "decide")
            await _handle_file_job(
                await _file_job_row(migrated_db, file_id), migrated_db,
                _cred_repo(migrated_db), service,
            )

        client.portal.call(place)

        call = model_stub.last("decide")
        assert "none" not in call["schema"]["properties"]["target"]["enum"]
        assert "<new_file>" in call["user_prompt"]
        assert f"Order number {number}" in call["user_prompt"]
        assert _file_status(client, migrated_db, file_id).json()["order_id"] == str(existing)

    def test_a_newsletter_named_file_is_still_placed_with_the_fake_provider(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        number: str,
    ) -> None:
        response = _upload(client, migrated_db, ticket_pdf(number), name="newsletter.pdf")
        file_id = uuid.UUID(response.json()["file_id"])
        order_id = client.portal.call(_place_unwritten, migrated_db, file_id)
        assert order_id is not None


class TestUploadLimits:
    def _limited(self, **changes: int) -> Any:
        base = get_config()
        limited = base.model_copy(
            update={"order_files": base.order_files.model_copy(update=changes)}
        )
        return patch("mail_verdict.api.orders.get_config", return_value=limited)

    def test_a_file_over_the_size_limit_is_refused_while_it_is_read(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
    ) -> None:
        with self._limited(max_file_bytes=500):
            response = _upload(client, migrated_db, ticket_pdf())
        assert response.status_code == 413

    def test_any_file_is_accepted_and_one_no_reader_knows_is_stored_and_listed_unread(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        number: str,
    ) -> None:
        word = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        uploads = {
            "letter.docx": (f"PK\x03\x04\x14\x00\x06\x00 {number}".encode(), word, "none"),
            "photo.heic": (
                b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00" + number.encode(),
                "image/heic", "none",
            ),
            "program.pdf": (b"MZ\x90\x00 program " + number.encode(), "application/pdf", "none"),
            "shared": (f"Gate 12, seat 14C, booking {number}".encode(), "", "text_layer"),
        }
        for name, (data, label, method) in uploads.items():
            response = _upload(client, migrated_db, data, name=name, content_type=label)
            assert response.status_code == 202, (name, response.text)
            file_id = uuid.UUID(response.json()["file_id"])

            order_id = client.portal.call(_place_unwritten, migrated_db, file_id)

            detail = _get(client, migrated_db, f"/orders/{order_id}").json()
            [document] = detail["documents"]
            assert (document["filename"], document["method"]) == (name, method)
            download = _get(client, migrated_db, document["download_url"].removeprefix("/api"))
            assert download.status_code == 200 and download.content == data
            assert "attachment" in download.headers["content-disposition"]
            assert download.headers["content-type"] == (
                "text/plain; charset=utf-8" if name == "shared" else
                "image/heic" if name == "photo.heic" else "application/octet-stream"
            )

    def test_an_entry_holds_a_bounded_number_and_volume_of_files(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
    ) -> None:
        async def entry() -> uuid.UUID:
            async with migrated_db.session() as session:
                return await repository.create_order(session)

        order_id = client.portal.call(entry)
        first = ticket_pdf(f"E{uuid.uuid4().int % 10**9:09d}")
        with self._limited(max_files_per_order=1):
            assert _upload(client, migrated_db, first, order_id=order_id).status_code == 202
            second = ticket_pdf(f"E{uuid.uuid4().int % 10**9:09d}")
            assert _upload(client, migrated_db, second, order_id=order_id).status_code == 413
        with self._limited(max_total_bytes_per_order=len(first) + 10):
            third = ticket_pdf(f"E{uuid.uuid4().int % 10**9:09d}")
            assert _upload(client, migrated_db, third, order_id=order_id).status_code == 413

    def test_an_unknown_entry_is_a_404_and_the_same_bytes_twice_are_one_file(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        number: str,
    ) -> None:
        assert _upload(
            client, migrated_db, ticket_pdf(number), order_id=uuid.uuid4(),
        ).status_code == 404
        pdf = ticket_pdf(number)
        first = _upload(client, migrated_db, pdf).json()
        again = _upload(client, migrated_db, pdf).json()
        assert first["file_id"] == again["file_id"]
        assert _file_status(client, migrated_db, uuid.uuid4()).status_code == 404

        async def jobs() -> int:
            async with migrated_db.session() as session:
                return int((await session.execute(
                    select(func.count(OrderJob.id)).where(
                        OrderJob.file_id == uuid.UUID(first["file_id"])
                    )
                )).scalar_one())

        assert client.portal.call(jobs) == 1


class TestDeletion:
    def test_deleting_a_file_keeps_the_entry_and_its_mail_and_queues_a_rewrite(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        number: str,
    ) -> None:
        async def entry_with_mail() -> tuple[uuid.UUID, uuid.UUID]:
            account_id, message_id, _, _, row = await _mail_with_pdf(migrated_db, number)
            service = await _settings_service(migrated_db)
            await _handle_mail_job(row, migrated_db, _cred_repo(migrated_db), service)
            return await _order_of(migrated_db, account_id), message_id

        order_id, message_id = client.portal.call(entry_with_mail)
        other = ticket_pdf(f"E{uuid.uuid4().int % 10**9:09d}", code="8801234567")
        response = _upload(client, migrated_db, other, order_id=order_id)
        file_id = uuid.UUID(response.json()["file_id"])
        client.portal.call(_place_unwritten, migrated_db, file_id)
        assert len(client.portal.call(_documents, migrated_db, order_id)) == 2

        with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
            response = client.delete(f"/orders/{order_id}/files/{file_id}")
        assert response.status_code == 204

        async def after() -> tuple[int, int, int, int]:
            async with migrated_db.session() as session:
                stored = (await session.execute(
                    select(func.count(OrderFile.id)).where(OrderFile.id == file_id)
                )).scalar_one()
                attachments = (await session.execute(
                    text("SELECT count(*) FROM attachments WHERE message_id = :m"),
                    {"m": message_id},
                )).scalar_one()
                rewrite = (await session.execute(
                    select(func.count(OrderJob.id)).where(
                        OrderJob.kind == "write", OrderJob.order_id == order_id,
                        OrderJob.status == "pending",
                    )
                )).scalar_one()
                file_count = (await session.execute(
                    select(Order.file_count).where(Order.id == order_id)
                )).scalar_one()
            return stored, attachments, rewrite, file_count

        assert client.portal.call(after) == (0, 1, 1, 0)
        documents = client.portal.call(_documents, migrated_db, order_id)
        assert [d.source for d in documents] == ["mail"]
        codes = [f for f in client.portal.call(_facts, migrated_db, order_id) if f.kind == "code"]
        assert len(codes) == 1 and codes[0].value == "5471205382"

    def test_deleting_the_last_file_of_a_file_only_entry_deletes_the_entry(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        number: str,
    ) -> None:
        file_id = uuid.UUID(_upload(client, migrated_db, ticket_pdf(number)).json()["file_id"])
        order_id = client.portal.call(_place_unwritten, migrated_db, file_id)

        with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
            assert client.delete(f"/orders/{order_id}/files/{file_id}").status_code == 204
            assert client.get(f"/orders/{order_id}").status_code == 404
            assert client.delete(f"/orders/{order_id}/files/{file_id}").status_code == 404

    def test_deleting_the_entry_deletes_its_files_and_documents_but_not_mail_attachments(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        number: str,
    ) -> None:
        async def entry_with_mail() -> tuple[uuid.UUID, uuid.UUID]:
            account_id, message_id, _, _, row = await _mail_with_pdf(migrated_db, number)
            await _handle_mail_job(
                row, migrated_db, _cred_repo(migrated_db), await _settings_service(migrated_db),
            )
            return await _order_of(migrated_db, account_id), message_id

        order_id, message_id = client.portal.call(entry_with_mail)
        pdf = ticket_pdf(f"E{uuid.uuid4().int % 10**9:09d}", code="9900112233")
        response = _upload(client, migrated_db, pdf, order_id=order_id)
        file_id = uuid.UUID(response.json()["file_id"])
        client.portal.call(_place_unwritten, migrated_db, file_id)

        with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
            assert client.delete(f"/orders/{order_id}").status_code == 204

        async def counts() -> tuple[int, int, int]:
            async with migrated_db.session() as session:
                return (
                    (await session.execute(
                        select(func.count(OrderFile.id)).where(OrderFile.id == file_id)
                    )).scalar_one(),
                    (await session.execute(
                        select(func.count(OrderDocument.id)).where(
                            OrderDocument.order_id == order_id
                        )
                    )).scalar_one(),
                    (await session.execute(
                        text("SELECT count(*) FROM attachments WHERE message_id = :m"),
                        {"m": message_id},
                    )).scalar_one(),
                )

        assert client.portal.call(counts) == (0, 0, 1)


class TestAnEntryMadeOnlyOfFiles:
    def test_it_is_written_found_by_its_numbers_and_closed_like_any_other(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        number: str,
    ) -> None:
        file_id = uuid.UUID(_upload(client, migrated_db, ticket_pdf(number)).json()["file_id"])
        order_id = client.portal.call(_place, migrated_db, file_id)

        async def checks() -> tuple[Any, list[str], str | None, list[uuid.UUID]]:
            async with migrated_db.session() as session:
                order = (
                    await session.execute(select(Order).where(Order.id == order_id))
                ).scalar_one()
                candidates = await find_candidates(
                    session, account_id=_ACCOUNT_FREE, thread_id=None, subject="Shipping",
                    from_addr="shop@example.com", haystack_raw=f"your booking {number} is ready",
                )
            known = await OrderLookup(migrated_db).known(
                account_id=_ACCOUNT_FREE, thread_id=None, text=f"re: {number}",
            )
            async with migrated_db.session() as session:
                await session.execute(
                    text("UPDATE order_files SET created_at = :old WHERE id = :f"),
                    {"old": datetime.now(timezone.utc) - timedelta(days=400), "f": file_id},
                )
                await repository.recompute_aggregates(session, order_id)
            closed = await close_overdue_orders(migrated_db, days=30, grace_days=0)
            return order, [
                reason for c in candidates if c.order_id == order_id for reason in c.reasons
            ], known, closed

        order, reasons, known, closed = client.portal.call(checks)
        assert order.written_at is not None and order.mail_count == 0 and order.file_count == 1
        assert any(r.startswith("mail contains") for r in reasons)
        assert known == "number"
        assert order_id in closed

    def test_a_write_job_for_it_is_not_skipped_for_having_no_mail(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        number: str,
    ) -> None:
        file_id = uuid.UUID(_upload(client, migrated_db, ticket_pdf(number)).json()["file_id"])
        order_id = client.portal.call(_place, migrated_db, file_id)

        async def outcome() -> str | None:
            async with migrated_db.session() as session:
                return (await session.execute(
                    select(OrderJob.outcome).where(
                        OrderJob.kind == "write", OrderJob.order_id == order_id,
                    )
                )).scalar_one()

        assert client.portal.call(outcome) == "written"


class TestFailureAndPruning:
    def test_a_file_whose_job_fails_for_good_is_marked_failed_and_pruned_after_its_retention(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        number: str,
    ) -> None:
        class Queue:
            failed: list[str] = []

            async def fail(self, item_id: object, *, worker_id: str, last_error: str) -> None:
                self.failed.append(last_error)

        pdfs = [ticket_pdf(f"E{uuid.uuid4().int % 10**9:09d}") for _ in range(3)]
        ids = [uuid.UUID(_upload(client, migrated_db, p).json()["file_id"]) for p in pdfs]

        async def run() -> tuple[list[str], int]:
            row = await _file_job_row(migrated_db, ids[0])
            await _fail(Queue(), row["id"], "w", "no model", db=migrated_db, row=row)
            second = await _file_job_row(migrated_db, ids[1])
            await _fail(Queue(), second["id"], "w", "no model", db=migrated_db, row=second)
            async with migrated_db.session() as session:
                await session.execute(
                    text("UPDATE order_files SET created_at = now() - interval '10 days' "
                         "WHERE id = ANY(:ids)"),
                    {"ids": [ids[0], ids[2]]},
                )
                deleted = await files.prune_failed_files(session, days=7)
                remaining = (await session.execute(
                    select(OrderFile.id, OrderFile.status).where(OrderFile.id.in_(ids))
                )).all()
            return [f"{r.id}:{r.status}" for r in remaining], deleted

        remaining, deleted = client.portal.call(run)
        # ids[0]: failed and old -> pruned; ids[1]: failed, recent -> kept;
        # ids[2]: pending and old -> kept (only failed files are pruned).
        assert deleted >= 1
        assert sorted(remaining) == sorted([f"{ids[1]}:failed", f"{ids[2]}:pending"])
        state = _file_status(client, migrated_db, ids[1]).json()
        assert (state["status"], state["last_error"]) == ("failed", "no model")


class _RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def complete(self, item_id: object, *, worker_id: str, status: str) -> None:
        self.calls.append(("complete", {"status": status}))

    async def fail(self, item_id: object, *, worker_id: str, last_error: str) -> None:
        self.calls.append(("fail", {"last_error": last_error}))

    async def retry(self, item_id: object, **kwargs: Any) -> None:
        self.calls.append(("retry", kwargs))


class TestTheQueueRunsFileJobs:
    def test_a_claimed_file_job_is_placed_and_completed(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        number: str,
    ) -> None:
        file_id = uuid.UUID(_upload(client, migrated_db, ticket_pdf(number)).json()["file_id"])
        queue = _RecordingQueue()

        async def run() -> None:
            service = await _settings_service(migrated_db)
            row = await _file_job_row(migrated_db, file_id)
            await _handle_item(
                {**row, "attempts": 1}, "w", queue, migrated_db, _cred_repo(migrated_db),
                service, None,
            )

        client.portal.call(run)

        assert queue.calls == [("complete", {"status": "done"})]
        assert _file_status(client, migrated_db, file_id).json()["status"] == "attached"

    def test_a_file_job_that_keeps_failing_ends_failed_after_its_last_attempt(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        number: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        file_id = uuid.UUID(_upload(client, migrated_db, ticket_pdf(number)).json()["file_id"])

        async def broken(*args: Any, **kwargs: Any) -> None:
            raise StageTransient("provider timed out")

        monkeypatch.setattr(worker, "_handle_file_job", broken)
        queue = _RecordingQueue()

        async def run(attempts: int) -> None:
            service = await _settings_service(migrated_db)
            row = await _file_job_row(migrated_db, file_id)
            await _handle_item(
                {**row, "attempts": attempts}, "w", queue, migrated_db,
                _cred_repo(migrated_db), service, None,
            )

        client.portal.call(run, 1)
        assert [c[0] for c in queue.calls] == ["retry"]
        assert _file_status(client, migrated_db, file_id).json()["status"] == "pending"

        client.portal.call(run, 5)
        assert [c[0] for c in queue.calls] == ["retry", "fail"]
        state = _file_status(client, migrated_db, file_id).json()
        assert (state["status"], state["last_error"]) == ("failed", "provider timed out")


class TestTheCodeImage:
    def test_a_code_fact_is_drawn_as_a_png_that_decodes_to_the_original_bytes(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        number: str,
    ) -> None:
        payload = bytes([0, 1, 2, 3, 200, 201, 255, 254]) + bytes(range(40, 140))
        pdf = ticket_pdf(number, code=payload, format_="Aztec")
        file_id = uuid.UUID(_upload(client, migrated_db, pdf).json()["file_id"])
        order_id = client.portal.call(_place_unwritten, migrated_db, file_id)
        detail = _get(client, migrated_db, f"/orders/{order_id}").json()
        code = next(f for f in detail["facts"] if f["kind"] == "code")
        assert code["code"]["payload_text"] is None

        response = _get(client, migrated_db, code["code"]["image_url"].removeprefix("/api"))
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        decoded = zxingcpp.read_barcodes(Image.open(io.BytesIO(response.content)))
        assert [(c.format.name, c.bytes) for c in decoded] == [("Aztec", payload)]

    def test_only_a_code_fact_of_that_entry_has_an_image(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        number: str,
    ) -> None:
        file_id = uuid.UUID(_upload(client, migrated_db, ticket_pdf(number)).json()["file_id"])
        order_id = client.portal.call(_place_unwritten, migrated_db, file_id)
        facts = client.portal.call(_facts, migrated_db, order_id)
        number_fact = next(f for f in facts if f.kind == "order_number")
        code_fact = next(f for f in facts if f.kind == "code")

        assert _get(
            client, migrated_db, f"/orders/{order_id}/codes/{number_fact.id}.png",
        ).status_code == 404
        assert _get(
            client, migrated_db, f"/orders/{uuid.uuid4()}/codes/{code_fact.id}.png",
        ).status_code == 404
        assert _get(
            client, migrated_db, f"/orders/{order_id}/codes/{code_fact.id}.png?scale=4",
        ).status_code == 200


class TestAFailedJobLeavesNothingInvisible:
    def _fail_write(
        self, client: TestClient, db: DatabaseConnection, order_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> _RecordingQueue:
        """Run the entry's pending write job against a model that refuses."""

        async def refuse(*args: Any, **kwargs: Any) -> None:
            raise StageRefused("declined")

        monkeypatch.setattr(worker.model_calls, "structured_call", refuse)
        queue = _RecordingQueue()

        async def run() -> None:
            service = await _stub_tasks(db, "write")
            async with db.session() as session:
                job_id = (await session.execute(
                    select(OrderJob.id).where(
                        OrderJob.kind == "write", OrderJob.order_id == order_id,
                        OrderJob.status == "pending",
                    )
                )).scalar_one()
            await _handle_item(
                {"id": job_id, "kind": "write", "order_id": order_id, "attempts": 1}, "w",
                queue, db, _cred_repo(db), service, None,
            )

        client.portal.call(run)
        return queue

    def test_a_first_write_that_fails_for_good_still_lists_the_entry_and_its_file(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        monkeypatch: pytest.MonkeyPatch, number: str,
    ) -> None:
        file_id = uuid.UUID(_upload(client, migrated_db, ticket_pdf(number)).json()["file_id"])
        order_id = client.portal.call(_place_unwritten, migrated_db, file_id)
        listed = _get(client, migrated_db, "/orders?limit=500").json()["items"]
        assert str(order_id) not in [i["id"] for i in listed]

        queue = self._fail_write(client, migrated_db, order_id, monkeypatch)

        assert [c[0] for c in queue.calls] == ["fail"]
        item = next(
            i for i in _get(client, migrated_db, "/orders?limit=500").json()["items"]
            if i["id"] == str(order_id)
        )
        assert (item["subject"], item["status"], item["text_stale"]) == (
            "ticket.pdf", "Needs a look", False,
        )
        detail = _get(client, migrated_db, f"/orders/{order_id}").json()
        assert [d["filename"] for d in detail["documents"]] == ["ticket.pdf"]
        with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
            assert client.delete(f"/orders/{order_id}/files/{file_id}").status_code == 204

    def test_a_rewrite_that_fails_for_good_stops_claiming_the_summary_is_being_updated(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        monkeypatch: pytest.MonkeyPatch, number: str,
    ) -> None:
        file_id = uuid.UUID(_upload(client, migrated_db, ticket_pdf(number)).json()["file_id"])
        order_id = client.portal.call(_place, migrated_db, file_id)

        async def stale() -> str:
            async with migrated_db.session() as session:
                await repository.mark_text_stale(session, order_id)
                await repository.enqueue_write_job(session, order_id, priority=10)
                return (await session.execute(
                    select(Order.summary).where(Order.id == order_id)
                )).scalar_one()

        written_summary = client.portal.call(stale)
        assert _get(client, migrated_db, f"/orders/{order_id}").json()["text_stale"] is True

        self._fail_write(client, migrated_db, order_id, monkeypatch)

        detail = _get(client, migrated_db, f"/orders/{order_id}").json()
        assert (detail["text_stale"], detail["summary"]) == (False, written_summary)

    def test_a_file_added_to_an_entry_that_fails_is_listed_there_and_can_be_deleted(
        self, client: TestClient, migrated_db: DatabaseConnection, orders_config: None,
        number: str,
    ) -> None:
        class Queue:
            async def fail(self, item_id: object, *, worker_id: str, last_error: str) -> None:
                return None

        async def entry() -> uuid.UUID:
            async with migrated_db.session() as session:
                order_id = await repository.create_order(session)
                await repository.write_order_text(
                    session, order_id, merchant="Rail", subject="Trip", status="ok",
                    is_open=True, icon="train", summary="s", model="fake",
                )
            return order_id

        order_id = client.portal.call(entry)
        pdf = ticket_pdf(number)
        file_id = uuid.UUID(_upload(client, migrated_db, pdf, order_id=order_id).json()["file_id"])

        async def fail() -> None:
            row = await _file_job_row(migrated_db, file_id)
            await _fail(Queue(), row["id"], "w", "no database", db=migrated_db, row=row)

        client.portal.call(fail)

        detail = _get(client, migrated_db, f"/orders/{order_id}").json()
        [document] = detail["documents"]
        assert (document["file_id"], document["method"]) == (str(file_id), "failed")
        download = _get(client, migrated_db, document["download_url"].removeprefix("/api"))
        assert download.content == pdf
        with patch("mail_verdict.api.orders.get_db_connection", return_value=migrated_db):
            assert client.delete(f"/orders/{order_id}/files/{file_id}").status_code == 204
