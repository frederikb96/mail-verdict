"""
The orders queue's worker: claims one job at a time and makes at most one
model call per job -- decide (which order, new, or none) for a `mail` job,
decide without "none" for a `file` job (a file the person added by hand),
write (title, status, summary, kind, key facts) for a `write` job.

A `mail` or `file` job reads the new item's attachments first, in the
extraction pool (orders/documents.py); their text reaches the decide call
and the number search, and is stored with the item once it joins an order.
A `write` job first makes sure every document of the order has been read by
the current extractor, so rewriting an order written before documents
existed repairs it.

Registered at concurrency 1 (queue_state's own default for a freshly
registered queue). Every job additionally runs inside one transaction
that opens with `SELECT pg_advisory_xact_lock(ORDERS_LOCK_KEY)`, held
for the reads, the model call and the writes -- this is what keeps
"never split" true by construction even if concurrency is later raised or
a second replica runs, not merely a consequence of concurrency 1. The
automatic close (orders/auto_close.py) takes the same lock, so a close
never interleaves with a mail being attached.

Never processed twice: a `mail` job whose (account_id, msg_key) already
has a row is refused at insert (uq_order_jobs_mail, see orders/
repository.py's enqueue_mail_job); one that is somehow re-claimed and
re-run finds its mail already in order_mails (checked before the decide
call below) and completes as a harmless no-op rather than attaching or
deciding again.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import AccountPrefs, Order, OrderFile, OrderJob, OrderMail
from mail_verdict.orders import documents, facts, model_calls, prompts, repository
from mail_verdict.orders.candidates import (
    Candidate,
    extract_labeled_identifiers,
    find_candidates,
)
from mail_verdict.orders.content import load_order_mail
from mail_verdict.orders.fake import fake_decide, fake_write
from mail_verdict.pipeline.context import current_verdict_for_mail
from mail_verdict.pipeline.contracts import (
    StageMisconfigured,
    StageThrottled,
    StageTransient,
    StageUnavailable,
)
from mail_verdict.queue.backoff import compute_backoff
from mail_verdict.queue.manager import QueueManager
from mail_verdict.queue.worker_loop import default_worker_loop

if TYPE_CHECKING:
    from sqlalchemy import Table

    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.settings.credentials import ProviderCredentialRepository
    from mail_verdict.settings.service import SettingsService

logger = logging.getLogger(__name__)

QUEUE_NAME = "orders"

# See this module's own docstring -- one key, no timer, so no second one.
ORDERS_LOCK_KEY = 761_035_200

# The write call includes every mail up to this count, oldest first;
# beyond it, the first 3 plus the newest 9 (design measured this on the
# longest real order seen, 15 mails, and it kept every fact). Past the
# prompt ceiling (documents.WRITE_PROMPT_CEILING_CHARS) middle mails go too.
_WRITE_ALL_MAILS_CEILING = 12

_DEFAULT_UNAVAILABLE_DELAY_S = 60.0


def register_orders(
    queue_manager: QueueManager,
    db: DatabaseConnection,
    cred_repo: ProviderCredentialRepository,
    settings_service: SettingsService,
    event_ring: EventRing | None,
) -> None:
    """Register the orders queue with the shared QueueManager. Nothing is
    started here -- queue_manager.start() drives it, the same as every
    other registered queue."""

    async def worker_body(worker_id: str, stop_event: asyncio.Event) -> None:
        work_queue = queue_manager.work_queue(QUEUE_NAME)
        settings = settings_service.get("orders")
        max_attempts = int(settings.get("max_attempts", 5))
        lease_seconds = float(settings.get("lease_seconds", 300))
        poll_interval = float(settings.get("poll_interval_seconds", 2.0))

        async def handle_item(row: Mapping[str, Any]) -> None:
            await _handle_item(
                row, worker_id, work_queue, db, cred_repo, settings_service, event_ring,
            )

        await default_worker_loop(
            work_queue, worker_id=worker_id, stop_event=stop_event, batch_size=1,
            lease_seconds=lease_seconds, handle_item=handle_item, poll_interval=poll_interval,
            max_attempts=max_attempts,
        )

    queue_manager.register(
        QUEUE_NAME, cast("Table", OrderJob.__table__), worker_body,
        # The write task's breaker: its suspension is what leaves orders
        # without text. The decide task trips its own, listed with the
        # others by GET /api/settings/providers.
        circuit_name=lambda: model_calls.write_circuit_name(settings_service),
    )


async def _handle_item(
    row: Mapping[str, Any],
    worker_id: str,
    work_queue: Any,
    db: DatabaseConnection,
    cred_repo: ProviderCredentialRepository,
    settings_service: SettingsService,
    event_ring: EventRing | None,
) -> None:
    item_id: uuid.UUID = row["id"]
    settings = settings_service.get("orders")

    try:
        if row["kind"] == "mail":
            broadcast = await _handle_mail_job(row, db, cred_repo, settings_service)
        elif row["kind"] == "file":
            broadcast = await _handle_file_job(row, db, cred_repo, settings_service)
        else:
            broadcast = await _handle_write_job(row, db, cred_repo, settings_service)
    except StageMisconfigured as exc:
        await _fail(work_queue, item_id, worker_id, str(exc), db=db, row=row)
        return
    except StageUnavailable as exc:
        await work_queue.release_untouched(item_id, worker_id=worker_id)
        await work_queue.retry(
            item_id, worker_id=worker_id,
            next_attempt_at=_in(_DEFAULT_UNAVAILABLE_DELAY_S), last_error=str(exc),
        )
        return
    except StageThrottled as exc:
        delay = exc.retry_after.total_seconds() if exc.retry_after else _DEFAULT_UNAVAILABLE_DELAY_S
        await work_queue.release_untouched(item_id, worker_id=worker_id)
        await work_queue.retry(
            item_id, worker_id=worker_id, next_attempt_at=_in(delay), last_error=str(exc),
        )
        return
    except StageTransient as exc:
        await _retry_or_fail(work_queue, item_id, worker_id, row, str(exc), settings, db=db)
        return
    except Exception as exc:  # noqa: BLE001 -- unknown failure, retry-then-fail
        await _retry_or_fail(
            work_queue, item_id, worker_id, row, f"unexpected: {exc}", settings, db=db,
        )
        return

    await work_queue.complete(item_id, worker_id=worker_id, status="done")
    if broadcast is not None and event_ring is not None:
        from mail_verdict.api.events import broadcast_event

        order_id, change = broadcast
        await broadcast_event(
            db, event_ring, "order.updated", {"order_id": str(order_id), "change": change},
        )


def _in(seconds: float) -> datetime:
    from datetime import timedelta

    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


async def _fail(
    work_queue: Any, item_id: uuid.UUID, worker_id: str, error: str,
    *, db: DatabaseConnection, row: Mapping[str, Any],
) -> None:
    await work_queue.fail(item_id, worker_id=worker_id, last_error=error)
    if row["kind"] == "file":
        # The file stays downloadable; its state says the placement failed.
        async with db.session() as session:
            await session.execute(
                update(OrderFile).where(OrderFile.id == row["file_id"])
                .values(status="failed", last_error=error[:500])
            )


async def _retry_or_fail(
    work_queue: Any, item_id: uuid.UUID, worker_id: str,
    row: Mapping[str, Any], error: str, settings: Mapping[str, Any],
    *, db: DatabaseConnection,
) -> None:
    max_attempts = int(settings.get("max_attempts", 5))
    if row["attempts"] >= max_attempts:
        await _fail(work_queue, item_id, worker_id, error, db=db, row=row)
        return
    delay = compute_backoff(
        row["attempts"],
        base_seconds=float(settings.get("base_delay_seconds", 5.0)),
        cap_seconds=float(settings.get("max_delay_seconds", 300.0)),
    )
    await work_queue.retry(
        item_id, worker_id=worker_id, next_attempt_at=_in(delay), last_error=error,
    )


async def _decide(
    db: DatabaseConnection, cred_repo: ProviderCredentialRepository,
    settings_service: SettingsService, *, candidates: list[Candidate], user_prompt: str,
    subject: str, allow_none: bool,
) -> tuple[dict[str, Any], str, float]:
    """The decide call (or the fake provider's answer), for a mail or a file."""
    if model_calls.is_fake(settings_service, "decide"):
        answer = fake_decide(subject=subject, candidates=candidates, allow_none=allow_none)
        return answer, "fake", 0.0
    handles = [f"C{i + 1}" for i in range(len(candidates))]
    return await model_calls.structured_call(
        db, cred_repo, settings_service, "decide",
        schema_name="order_decision", system_prompt=prompts.load_decide_system_prompt(),
        user_prompt=user_prompt, schema=prompts.build_decide_schema(handles, allow_none=allow_none),
        validate=lambda data: prompts.validate_decide_response(
            data, handles=handles, allow_none=allow_none,
        ),
    )


def _identifiers(
    decision: dict[str, Any], *, own_text: str, attachment_text: str,
) -> list[tuple[str, str]]:
    """The identifiers to store for what the decide call placed: its own
    answer, or -- when it gave none -- the deterministic rescan of the
    item's own text (the one field a small model measurably misses even
    when the number is plainly present: a floor, never a second opinion
    overriding a call that did answer). The attachments are always
    rescanned: a number that only a PDF names (a carrier's tracking number
    in a shop's shipping notice) is what a later mail is matched on."""
    found = [
        (item["kind"], item["value"]) for item in decision.get("identifiers", [])
        if isinstance(item, dict) and item.get("kind") and item.get("value")
    ]
    if not found:
        found = extract_labeled_identifiers(own_text)
    return [*found, *extract_labeled_identifiers(attachment_text)]


async def _handle_mail_job(
    row: Mapping[str, Any],
    db: DatabaseConnection,
    cred_repo: ProviderCredentialRepository,
    settings_service: SettingsService,
) -> tuple[uuid.UUID, str] | None:
    """Decide where one mail belongs. Returns (order_id, "updated") when
    an already-written order gained a mail, else None -- a brand new
    order broadcasts nothing until its first write (see 6.4 in the
    design this mirrors: "created" is sent only once an order has text)."""
    account_id: uuid.UUID = row["account_id"]
    msg_key: str = row["msg_key"]
    message_id: uuid.UUID | None = row["message_id"]
    origin: str = row["origin"]
    filter_reason: str = row.get("filter_reason") or ""

    async with db.session() as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ORDERS_LOCK_KEY})

        content = await load_order_mail(
            session, account_id=account_id, msg_key=msg_key, message_id=message_id,
        )
        if content is None:
            await _set_outcome(session, row["id"], outcome="skipped", last_error="message gone")
            return None

        prefs_result = await session.execute(
            select(AccountPrefs.orders_enabled).where(AccountPrefs.account_id == account_id)
        )
        if not (prefs_result.scalar_one_or_none() or False):
            await _set_outcome(session, row["id"], outcome="skipped", last_error="orders disabled")
            return None

        verdict = await current_verdict_for_mail(db, content.message_id)
        if verdict is not None and verdict.is_spam:
            await _set_outcome(session, row["id"], outcome="skipped", last_error="spam")
            return None

        already = await session.execute(
            select(OrderMail.order_id).where(
                OrderMail.account_id == account_id, OrderMail.msg_key == msg_key,
            )
        )
        existing_order_id = already.scalar_one_or_none()
        if existing_order_id is not None:
            await _set_outcome(session, row["id"], outcome="attached")
            return None

        reads = await documents.read_mail_documents(session, content.attachments)
        attachment_text = documents.attachment_haystack(reads)
        candidates = await find_candidates(
            session, account_id=account_id, thread_id=content.thread_id,
            subject=content.subject, from_addr=content.from_addr,
            haystack_raw=f"{content.body.raw}\n{attachment_text}",
        )
        user_prompt = prompts.build_decide_user_prompt(
            subject=content.subject, from_addr=content.from_addr, to_addrs=content.to_addrs,
            received_at=_isoformat(content.received_at),
            attachments=[(a.filename, a.content_type) for a in content.attachments],
            body=content.body.text, candidates=candidates,
            documents=documents.decide_documents(reads),
        )
        decision, model_name, latency_ms = await _decide(
            db, cred_repo, settings_service, candidates=candidates, user_prompt=user_prompt,
            subject=content.subject, allow_none=True,
        )

        target = decision["target"]
        if target == "none":
            await _set_outcome(
                session, row["id"], outcome="none", decision=decision,
                model=model_name, latency_ms=int(latency_ms),
            )
            return None

        was_new = target == "new"
        if was_new:
            order_id = await repository.create_order(session)
        else:
            handle_map = {f"C{i + 1}": c.order_id for i, c in enumerate(candidates)}
            order_id = handle_map[target]

        already_written = False
        if not was_new:
            written_result = await session.execute(
                select(Order.written_at).where(Order.id == order_id)
            )
            written_row = written_result.one_or_none()
            already_written = written_row is not None and written_row.written_at is not None

        attached_by = "thread" if origin == "thread" else "ai"
        attached = await repository.attach_mail(
            session, order_id=order_id, account_id=account_id, msg_key=msg_key,
            message_id=content.message_id, thread_id=content.thread_id,
            subject=content.subject, from_addr=content.from_addr,
            received_at=content.received_at or datetime.now(timezone.utc),
            attached_by=attached_by,
        )
        if attached:
            await repository.hand_open_state_to_model(session, order_id)
            await documents.store_mail_documents(
                session, order_id=order_id, account_id=account_id, msg_key=msg_key, reads=reads,
            )
        await repository.store_identifier_facts(
            session, order_id,
            _identifiers(
                decision, own_text=f"{content.subject}\n{content.body.raw}",
                attachment_text=attachment_text,
            ),
        )
        await facts.reconcile_code_facts(session, order_id)
        await repository.recompute_aggregates(session, order_id)
        await repository.mark_text_stale(session, order_id)
        write_priority = 0 if filter_reason == "thread" else 50
        await repository.enqueue_write_job(
            session, order_id, priority=write_priority, origin=origin,
        )
        await _set_outcome(
            session, row["id"], outcome="created" if was_new else "attached",
            decision=decision, model=model_name, latency_ms=int(latency_ms), order_id=order_id,
        )

        return (order_id, "updated") if already_written else None


async def _handle_file_job(
    row: Mapping[str, Any],
    db: DatabaseConnection,
    cred_repo: ProviderCredentialRepository,
    settings_service: SettingsService,
) -> tuple[uuid.UUID, str] | None:
    """Place a file the person added by hand: read it, then join the entry
    it was added to, or let the decide call pick an entry or open a new one
    (never "none" -- the person added it on purpose). Returns (order_id,
    "updated") when an already-written order gained the file, else None."""
    file_id: uuid.UUID = row["file_id"]

    async with db.session() as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ORDERS_LOCK_KEY})

        file_result = await session.execute(
            select(
                OrderFile.order_id, OrderFile.filename, OrderFile.content_type, OrderFile.status,
                OrderFile.created_at,
            ).where(OrderFile.id == file_id)
        )
        file_row = file_result.one_or_none()
        if file_row is None:
            await _set_outcome(session, row["id"], outcome="skipped", last_error="file gone")
            return None
        if file_row.status == "attached":
            await _set_outcome(session, row["id"], outcome="attached", order_id=file_row.order_id)
            return None

        read = await documents.read_file_document(
            session, file_id=file_id, filename=file_row.filename,
            content_type=file_row.content_type,
        )
        if read is None:
            await _set_outcome(session, row["id"], outcome="skipped", last_error="file gone")
            return None
        attachment_text = documents.attachment_haystack([read])

        decision: dict[str, Any] = {}
        model_name, latency_ms = None, 0.0
        candidates: list[Candidate] = []
        order_id: uuid.UUID
        if file_row.order_id is not None:
            order_id = file_row.order_id
            was_new = False
        else:
            candidates = await find_candidates(
                session, account_id=None, thread_id=None, subject=file_row.filename,
                from_addr="", haystack_raw=attachment_text,
            )
            user_prompt = prompts.build_decide_file_prompt(
                added_at=_isoformat(file_row.created_at),
                documents=documents.decide_documents([read]), candidates=candidates,
            )
            decision, model_name, latency_ms = await _decide(
                db, cred_repo, settings_service, candidates=candidates, user_prompt=user_prompt,
                subject=file_row.filename, allow_none=False,
            )
            was_new = decision["target"] == "new"
            if was_new:
                order_id = await repository.create_order(session)
            else:
                order_id = {f"C{i + 1}": c.order_id for i, c in enumerate(candidates)}[
                    decision["target"]
                ]

        written = await session.execute(select(Order.written_at).where(Order.id == order_id))
        written_row = written.one_or_none()
        if written_row is None:
            # The entry it was added to is gone: nothing to attach to.
            await _set_outcome(session, row["id"], outcome="skipped", last_error="order gone")
            return None
        already_written = written_row.written_at is not None

        await session.execute(
            update(OrderFile).where(OrderFile.id == file_id)
            .values(order_id=order_id, status="attached", last_error=None)
        )
        await documents.store_file_document(session, order_id=order_id, read=read)
        await repository.hand_open_state_to_model(session, order_id)
        await repository.store_identifier_facts(
            session, order_id,
            _identifiers(decision, own_text=file_row.filename, attachment_text=attachment_text),
        )
        await facts.reconcile_code_facts(session, order_id)
        await repository.recompute_aggregates(session, order_id)
        await repository.mark_text_stale(session, order_id)
        await repository.enqueue_write_job(session, order_id, priority=5, origin="upload")
        await _set_outcome(
            session, row["id"], outcome="created" if was_new else "attached",
            decision=decision or None, model=model_name, latency_ms=int(latency_ms),
            order_id=order_id,
        )
        return (order_id, "updated") if already_written else None


def _isoformat(value: datetime | None) -> str:
    return value.isoformat() if value else ""


async def _set_outcome(
    session: AsyncSession, job_id: uuid.UUID, *, outcome: str,
    last_error: str | None = None, decision: dict[str, Any] | None = None,
    model: str | None = None, latency_ms: int | None = None,
    order_id: uuid.UUID | None = None,
) -> None:
    """Record the domain outcome on a job row -- distinct from the row's
    WorkQueue `status`, which work_queue.complete()/fail() manage once
    this (and this function's own transaction) has committed."""
    values: dict[str, Any] = {"outcome": outcome}
    if last_error is not None:
        values["last_error"] = last_error
    if decision is not None:
        values["decision"] = decision
    if model is not None:
        values["model"] = model
    if latency_ms is not None:
        values["latency_ms"] = latency_ms
    if order_id is not None:
        values["order_id"] = order_id
    await session.execute(update(OrderJob).where(OrderJob.id == job_id).values(**values))


async def _handle_write_job(
    row: Mapping[str, Any],
    db: DatabaseConnection,
    cred_repo: ProviderCredentialRepository,
    settings_service: SettingsService,
) -> tuple[uuid.UUID, str] | None:
    order_id: uuid.UUID = row["order_id"]
    orders_settings = settings_service.get("orders")
    language = str(orders_settings.get("language", "English"))

    async with db.session() as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ORDERS_LOCK_KEY})

        order_result = await session.execute(
            select(
                Order.merchant, Order.subject, Order.status, Order.summary, Order.written_at,
                Order.mail_count, Order.file_count,
            ).where(Order.id == order_id)
        )
        order_row = order_result.one_or_none()
        if order_row is None or order_row.mail_count + order_row.file_count == 0:
            await _set_outcome(session, row["id"], outcome="skipped")
            return None

        mails_result = await session.execute(
            select(
                OrderMail.account_id, OrderMail.msg_key, OrderMail.message_id,
                OrderMail.subject, OrderMail.from_addr, OrderMail.received_at,
            )
            .where(OrderMail.order_id == order_id)
            .order_by(OrderMail.received_at)
        )
        mail_rows = mails_result.all()
        included = (
            list(mail_rows[:3]) + list(mail_rows[-9:])
            if len(mail_rows) > _WRITE_ALL_MAILS_CEILING else list(mail_rows)
        )
        included_keys = {(m.account_id, m.msg_key) for m in included}

        # Every mail of the order, not only those the prompt shows: a code on
        # an older mail's ticket is still a fact of the entry.
        mails: list[dict[str, Any]] = []
        for mail_row in mail_rows:
            content = await load_order_mail(
                session, account_id=mail_row.account_id, msg_key=mail_row.msg_key,
                message_id=mail_row.message_id,
            )
            if content is not None:
                await documents.heal_mail_documents(
                    session, order_id=order_id, account_id=mail_row.account_id,
                    msg_key=mail_row.msg_key, attachments=content.attachments,
                )
            if (mail_row.account_id, mail_row.msg_key) in included_keys:
                mails.append({
                    "account_id": mail_row.account_id, "msg_key": mail_row.msg_key,
                    "received_at": _isoformat(mail_row.received_at),
                    "from_addr": content.from_addr if content else (mail_row.from_addr or ""),
                    "subject": content.subject if content else (mail_row.subject or ""),
                    "body": content.body.text if content else "",
                })
        await documents.heal_file_documents(session, order_id)
        write_input = documents.assemble_write_input(
            mails, await documents.load_stored_documents(session, order_id),
        )

        current_text = None
        if order_row.written_at is not None:
            current_text = {
                "merchant": order_row.merchant, "subject": order_row.subject,
                "status": order_row.status, "summary": order_row.summary,
            }

        if model_calls.is_fake(settings_service, "write"):
            answer = fake_write(mails=write_input.mails, files=write_input.files)
            model_name = "fake"
            latency_ms = 0.0
        else:
            user_prompt = prompts.build_write_user_prompt(
                current_text=current_text, mails=write_input.mails, files=write_input.files,
            )
            answer, model_name, latency_ms = await model_calls.structured_call(
                db, cred_repo, settings_service, "write",
                schema_name="order_text",
                system_prompt=prompts.build_write_system_prompt(language=language),
                user_prompt=user_prompt, schema=prompts.WRITE_SCHEMA,
                validate=prompts.validate_write_response,
            )

        result = await repository.write_order_text(
            session, order_id, merchant=answer["merchant"], subject=answer["subject"],
            status=answer["status"], is_open=bool(answer["open"]), icon=answer["icon"],
            summary=answer["summary"], model=model_name,
            expected_until=prompts.parse_iso_date(answer.get("expected_until")),
            kind=answer["kind"], event_start=prompts.parse_iso_date(answer.get("event_start")),
            event_end=prompts.parse_iso_date(answer.get("event_end")),
        )
        await facts.apply_written_facts(
            session, order_id, facts.parse_written_facts(answer.get("facts")),
            write_input.code_map,
        )
        await _set_outcome(
            session, row["id"], outcome="written", model=model_name, latency_ms=int(latency_ms),
        )
        if result is None:
            return None
        return (order_id, "created" if result.created else "updated")
