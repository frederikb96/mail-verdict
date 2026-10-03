"""
The webhook queue against a real database and a local HTTP stub: one POST
of the unmodified raw bytes with the resolved secret header, each response
class (2xx final, 5xx retried then final, 4xx not retried, exhausted retries
alerting), a mail never queued twice, a failed delivery re-queued by hand,
and the backfill sending existing mail oldest first exactly once.
"""

from __future__ import annotations

import itertools
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.config.loader import WebhooksConfig
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import WebhookDelivery
from mail_verdict.pipeline.contracts import StageDefinition, Webhook
from mail_verdict.pipeline.effect_codec import parse_effect
from mail_verdict.pipeline.effects import apply_effects
from mail_verdict.pipeline.message_view import load_message_view
from mail_verdict.queue.work_queue import WorkQueue
from mail_verdict.settings.secret_store import SecretRepository
from mail_verdict.webhooks import repository
from mail_verdict.webhooks.backfill import BACKFILL_PRIORITY, backfill_webhook
from mail_verdict.webhooks.worker import handle_delivery

pytestmark = pytest.mark.asyncio

_KEY = "0123456789abcdef" * 4
_TOKEN = "tok-NOT-A-REAL-TOKEN-1234"
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_uid = itertools.count(1)
_RAW = b"From: kitchen@example.com\r\nSubject: Guten Appetit\r\n\r\nSchnitzel \xc3\xa4\x00\xff\r\n"


class _Stub:
    """Records every request and answers from a scripted status list."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.requests: list[dict[str, Any]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                stub.requests.append({
                    "path": self.path, "headers": dict(self.headers.items()),
                    "body": self.rfile.read(length),
                })
                status = stub.statuses.pop(0) if len(stub.statuses) > 1 else stub.statuses[0]
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/api/canteen/mails"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def stub_factory() -> Iterator[Any]:
    stubs: list[_Stub] = []

    def make(statuses: list[int]) -> _Stub:
        stubs.append(_Stub(statuses))
        return stubs[-1]

    yield make
    for s in stubs:
        s.close()


def _cfg(max_attempts: int = 3) -> WebhooksConfig:
    return WebhooksConfig(
        request_timeout_seconds=5, max_attempts=max_attempts, base_delay_seconds=0,
        max_delay_seconds=0, max_body_bytes=1_000_000, poll_interval_seconds=0.1,
        lease_seconds=60,
    )


def _effect(url: str, name: str = "canteen") -> Webhook:
    effect = parse_effect({"webhook": {
        "name": name, "url": url, "received_at_param": "received_at",
        "headers": {"Authorization": "Bearer {{secret:CANTEEN_TOKEN}}"},
    }})
    assert isinstance(effect, Webhook)
    return effect


async def _seed_account(session: AsyncSession) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'u@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    return account_id


async def _seed_folder(
    session: AsyncSession, account_id: uuid.UUID, special_use: str | None = None,
) -> uuid.UUID:
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use) "
            "VALUES (:id, :a, :n, :s)"
        ),
        {"id": folder_id, "a": account_id, "n": special_use or "INBOX", "s": special_use},
    )
    return folder_id


async def _seed_message(
    session: AsyncSession, account_id: uuid.UUID, folder_id: uuid.UUID, *,
    subject: str = "Guten Appetit", received_at: datetime = _NOW,
    raw: bytes | None = _RAW,
) -> tuple[uuid.UUID, str]:
    message_id, header = uuid.uuid4(), f"<{uuid.uuid4()}@example.com>"
    await session.execute(
        text(
            "INSERT INTO messages (id, account_id, folder_id, imap_uid, thread_id, message_id, "
            "from_addr, subject, body_text, received_at, size_bytes, is_seen, raw_source) "
            "VALUES (:id, :a, :f, :uid, :t, :h, 'kitchen@example.com', :s, 'Schnitzel', "
            ":r, 512, false, :raw)"
        ),
        {
            "id": message_id, "a": account_id, "f": folder_id, "uid": next(_uid),
            "t": uuid.uuid4(), "h": header, "s": subject, "r": received_at, "raw": raw,
        },
    )
    return message_id, header


async def _setup(
    db: DatabaseConnection, *, raw: bytes | None = _RAW,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    async with db.session() as session:
        account_id = await _seed_account(session)
        folder_id = await _seed_folder(session, account_id)
        message_id, header = await _seed_message(session, account_id, folder_id, raw=raw)
    return account_id, message_id, header


async def _enqueue(
    db: DatabaseConnection, effect: Webhook, account_id: uuid.UUID, message_id: uuid.UUID,
    header: str,
) -> bool:
    async with db.session() as session:
        return await repository.enqueue_delivery(
            session, effect, account_id=account_id, msg_key=header, message_id=message_id,
            origin="live", priority=0,
        )


async def _drain(db: DatabaseConnection, secrets: SecretRepository, cfg: WebhooksConfig) -> int:
    """Claim and handle until nothing is due, as the worker loop would."""
    queue = WorkQueue(db, WebhookDelivery.__table__)  # type: ignore[arg-type]
    handled = 0
    while rows := await queue.claim_batch(
        worker_id="t", batch_size=1, lease_seconds=60, max_attempts=cfg.max_attempts,
    ):
        for row in rows:
            await handle_delivery(row, "t", queue, db, secrets, cfg, None, None)
            handled += 1
    return handled


async def _row(db: DatabaseConnection, account_id: uuid.UUID) -> Any:
    async with db.session() as session:
        return (
            await session.execute(
                text("SELECT * FROM webhook_deliveries WHERE account_id = :a"), {"a": account_id},
            )
        ).one()


async def _alerts(db: DatabaseConnection, account_id: uuid.UUID) -> list[Any]:
    async with db.session() as session:
        return list(
            (
                await session.execute(
                    text("SELECT kind, title, body FROM alerts WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).all()
        )


async def _secrets(db: DatabaseConnection, *, set_token: bool = True) -> SecretRepository:
    repo = SecretRepository(db, _KEY)
    if set_token:
        await repo.put("CANTEEN_TOKEN", _TOKEN)
    else:
        await repo.delete("CANTEEN_TOKEN")
    return repo


async def test_success_posts_the_raw_bytes_once_with_the_resolved_header(
    migrated_db: DatabaseConnection, stub_factory: Any,
) -> None:
    stub = stub_factory([201])
    secrets = await _secrets(migrated_db)
    account_id, message_id, header = await _setup(migrated_db)
    effect = _effect(stub.url)

    assert await _enqueue(migrated_db, effect, account_id, message_id, header) is True
    assert await _drain(migrated_db, secrets, _cfg()) == 1

    assert len(stub.requests) == 1
    request = stub.requests[0]
    assert request["body"] == _RAW
    assert request["headers"]["Content-Type"] == "message/rfc822"
    assert request["headers"]["Authorization"] == f"Bearer {_TOKEN}"
    query = parse_qs(urlsplit(request["path"]).query)
    assert query == {"received_at": ["2026-06-01T12:00:00+00:00"]}

    row = await _row(migrated_db, account_id)
    assert (row.status, row.http_status, row.attempts) == ("done", 201, 1)
    assert row.delivered_at is not None

    # Delivered mail is never queued again, nor claimed again.
    assert await _enqueue(migrated_db, effect, account_id, message_id, header) is False
    assert await _drain(migrated_db, secrets, _cfg()) == 0
    assert len(stub.requests) == 1
    assert await _alerts(migrated_db, account_id) == []


async def test_a_5xx_is_retried_and_the_success_is_final(
    migrated_db: DatabaseConnection, stub_factory: Any,
) -> None:
    stub = stub_factory([503, 201])
    secrets = await _secrets(migrated_db)
    account_id, message_id, header = await _setup(migrated_db)
    await _enqueue(migrated_db, _effect(stub.url), account_id, message_id, header)

    await _drain(migrated_db, secrets, _cfg())

    assert len(stub.requests) == 2
    row = await _row(migrated_db, account_id)
    assert (row.status, row.http_status, row.attempts) == ("done", 201, 2)
    assert await _alerts(migrated_db, account_id) == []


async def test_a_4xx_is_not_retried_and_raises_an_alert_then_can_be_requeued(
    migrated_db: DatabaseConnection, stub_factory: Any,
) -> None:
    stub = stub_factory([400, 201])
    secrets = await _secrets(migrated_db)
    account_id, message_id, header = await _setup(migrated_db)
    effect = _effect(stub.url)
    await _enqueue(migrated_db, effect, account_id, message_id, header)

    await _drain(migrated_db, secrets, _cfg())

    assert len(stub.requests) == 1
    row = await _row(migrated_db, account_id)
    assert (row.status, row.http_status, row.last_error) == ("failed", 400, "HTTP 400")
    alerts = await _alerts(migrated_db, account_id)
    assert [a.kind for a in alerts] == ["webhook_failed"]
    assert "HTTP 400" in alerts[0].body
    assert _TOKEN not in alerts[0].title + alerts[0].body

    # A failed row still blocks the rule from queueing the same mail again.
    assert await _enqueue(migrated_db, effect, account_id, message_id, header) is False

    async with migrated_db.session() as session:
        assert await repository.requeue_failed(session, row.id) is True
    await _drain(migrated_db, secrets, _cfg())
    assert len(stub.requests) == 2
    assert (await _row(migrated_db, account_id)).status == "done"


async def test_requeue_only_moves_a_failed_delivery(
    migrated_db: DatabaseConnection, stub_factory: Any,
) -> None:
    stub = stub_factory([201])
    secrets = await _secrets(migrated_db)
    account_id, message_id, header = await _setup(migrated_db)
    await _enqueue(migrated_db, _effect(stub.url), account_id, message_id, header)
    await _drain(migrated_db, secrets, _cfg())
    row = await _row(migrated_db, account_id)

    async with migrated_db.session() as session:
        assert await repository.requeue_failed(session, row.id) is False
    await _drain(migrated_db, secrets, _cfg())
    assert len(stub.requests) == 1


async def test_exhausted_retries_fail_and_alert(
    migrated_db: DatabaseConnection, stub_factory: Any,
) -> None:
    stub = stub_factory([500])
    secrets = await _secrets(migrated_db)
    account_id, message_id, header = await _setup(migrated_db)
    await _enqueue(migrated_db, _effect(stub.url), account_id, message_id, header)

    await _drain(migrated_db, secrets, _cfg(max_attempts=3))

    assert len(stub.requests) == 3
    row = await _row(migrated_db, account_id)
    assert (row.status, row.attempts) == ("failed", 3)
    alerts = await _alerts(migrated_db, account_id)
    assert len(alerts) == 1
    assert "gave up after 3 attempts" in alerts[0].body


async def test_a_refused_connection_is_a_transient_failure(
    migrated_db: DatabaseConnection, stub_factory: Any,
) -> None:
    stub = stub_factory([201])
    url = stub.url
    stub.close()
    secrets = await _secrets(migrated_db)
    account_id, message_id, header = await _setup(migrated_db)
    await _enqueue(migrated_db, _effect(url), account_id, message_id, header)

    await _drain(migrated_db, secrets, _cfg(max_attempts=2))

    row = await _row(migrated_db, account_id)
    assert (row.status, row.attempts) == ("failed", 2)
    assert "ConnectError" in row.last_error
    assert _TOKEN not in row.last_error


async def test_a_missing_secret_makes_no_request_and_names_only_the_secret(
    migrated_db: DatabaseConnection, stub_factory: Any,
) -> None:
    stub = stub_factory([201])
    secrets = await _secrets(migrated_db, set_token=False)
    account_id, message_id, header = await _setup(migrated_db)
    await _enqueue(migrated_db, _effect(stub.url), account_id, message_id, header)

    await _drain(migrated_db, secrets, _cfg())

    assert stub.requests == []
    row = await _row(migrated_db, account_id)
    assert row.status == "failed"
    assert "CANTEEN_TOKEN" in row.last_error
    assert len(await _alerts(migrated_db, account_id)) == 1


async def test_a_message_whose_raw_source_was_never_stored_fails_without_a_request(
    migrated_db: DatabaseConnection, stub_factory: Any,
) -> None:
    stub = stub_factory([201])
    secrets = await _secrets(migrated_db)
    account_id, message_id, header = await _setup(migrated_db, raw=None)
    await _enqueue(migrated_db, _effect(stub.url), account_id, message_id, header)

    await _drain(migrated_db, secrets, _cfg())

    assert stub.requests == []
    assert (await _row(migrated_db, account_id)).status == "failed"
    assert len(await _alerts(migrated_db, account_id)) == 1


async def test_a_vanished_message_is_skipped_quietly(
    migrated_db: DatabaseConnection, stub_factory: Any,
) -> None:
    stub = stub_factory([201])
    secrets = await _secrets(migrated_db)
    account_id, message_id, header = await _setup(migrated_db)
    await _enqueue(migrated_db, _effect(stub.url), account_id, message_id, header)
    async with migrated_db.session() as session:
        await session.execute(
            text("UPDATE messages SET expunged_at = now() WHERE id = :id"), {"id": message_id},
        )

    await _drain(migrated_db, secrets, _cfg())

    assert stub.requests == []
    assert (await _row(migrated_db, account_id)).status == "skipped"
    assert await _alerts(migrated_db, account_id) == []


async def test_the_rule_effect_queues_one_delivery_and_a_dry_run_queues_none(
    migrated_db: DatabaseConnection,
) -> None:
    account_id, message_id, _ = await _setup(migrated_db)
    effect = _effect("http://127.0.0.1:9/never")
    async with migrated_db.session() as session:
        view = await load_message_view(session, message_id)
    assert view is not None

    for apply, expected in ((False, 0), (True, 1), (True, 1)):
        _, applied = await apply_effects(
            migrated_db, view, (effect,), apply=apply, folders=None,  # type: ignore[arg-type]
            event_ring=None, stage_id="canteen",
        )
        assert len(applied) == 1
        async with migrated_db.session() as session:
            count = (
                await session.execute(
                    text("SELECT count(*) FROM webhook_deliveries WHERE account_id = :a"),
                    {"a": account_id},
                )
            ).scalar_one()
        assert count == expected
    assert applied[0].applied is False
    assert "already queued" in applied[0].detail


async def test_backfill_queues_matches_oldest_first_and_only_once(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        inbox = await _seed_folder(session, account_id)
        junk = await _seed_folder(session, account_id, special_use="junk")
        trash = await _seed_folder(session, account_id, special_use="trash")
        archive = await _seed_folder(session, account_id, special_use="archive")
        # Inserted newest first, so insertion order cannot pass for received order.
        newest, _ = await _seed_message(
            session, account_id, inbox, received_at=_NOW + timedelta(days=2))
        oldest, _ = await _seed_message(session, account_id, inbox, received_at=_NOW)
        middle, _ = await _seed_message(
            session, account_id, inbox, received_at=_NOW + timedelta(days=1))
        await _seed_message(
            session, account_id, inbox, subject="Something else", received_at=_NOW)
        await _seed_message(session, account_id, junk, received_at=_NOW)
        await _seed_message(session, account_id, trash, received_at=_NOW)
        archived, _ = await _seed_message(
            session, account_id, archive, received_at=_NOW + timedelta(days=3))
        await _seed_message(
            session, account_id, inbox, received_at=_NOW - timedelta(days=30))  # before `since`

    stage = StageDefinition(
        stage_id="canteen", type="match", name="canteen",
        config={
            "when": {"subject_contains": "Guten Appetit"},
            "effects": [{"webhook": {
                "name": "canteen", "url": "http://127.0.0.1:9/never",
                "headers": {"Authorization": "Bearer {{secret:CANTEEN_TOKEN}}"},
            }}],
        },
        accounts=(account_id,),
    )
    since = _NOW - timedelta(days=1)

    dry = await backfill_webhook(
        migrated_db, (stage,), "canteen", since=since, until=None, limit=100, dry_run=True)
    assert (dry.matched, dry.queued, dry.already_queued) == (4, 4, 0)
    async with migrated_db.session() as session:
        assert not [
            r for r in await repository.list_deliveries(
                session, name="canteen", status=None, limit=500)
            if r.account_id == account_id
        ]

    first = await backfill_webhook(
        migrated_db, (stage,), "canteen", since=since, until=None, limit=100, dry_run=False)
    assert (first.scanned, first.matched, first.queued, first.already_queued) == (7, 4, 4, 0)
    assert first.truncated is False

    async with migrated_db.session() as session:
        rows = [
            r for r in await repository.list_deliveries(
                session, name="canteen", status="pending", limit=50)
            if r.account_id == account_id
        ]
    by_due = [r.message_id for r in sorted(rows, key=lambda r: r.next_attempt_at)]
    assert by_due == [oldest, middle, newest, archived]
    assert {r.priority for r in rows} == {BACKFILL_PRIORITY}
    assert {r.origin for r in rows} == {"backfill"}

    second = await backfill_webhook(
        migrated_db, (stage,), "canteen", since=since, until=None, limit=100, dry_run=False)
    assert (second.matched, second.queued, second.already_queued) == (4, 0, 4)


async def test_backfill_of_an_unknown_webhook_name_is_an_error(
    migrated_db: DatabaseConnection,
) -> None:
    from mail_verdict.webhooks.backfill import WebhookNotConfiguredError

    with pytest.raises(WebhookNotConfiguredError):
        await backfill_webhook(
            migrated_db, (), "nope", since=_NOW, until=None, limit=10, dry_run=True)
