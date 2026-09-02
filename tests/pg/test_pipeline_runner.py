"""
The pipeline runner against a real database: proves the full path from a
claimed `pipeline_runs` row through the migrated default definition
(classify + move-spam) to a recorded verdict and an applied move, plus the
invariants the design calls load-bearing -- never classify twice,
`classify` never runs on historical mail, and a guarded effect against a
message that vanished mid-run leaves nothing applied.

`migrated_db` carries the first pipeline revision from alembic migration
0006, built from `settings.rules`/`settings.spam`'s defaults (empty
rules, spam enabled, auto-move-to-junk with auto-mark-read) -- exactly
what a fresh deployment gets. pipeline_revisions is append-only and
shared with every other pg test in this session, though, so `_make_runner`
appends a fresh copy of that same definition rather than trusting
whatever the current (max-revision) definition happens to be by the time
this file runs -- the pipeline configuration API's own tests write several
other definitions to this same table.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.repository import AccountPrefsRepository, VerdictRepository
from mail_verdict.pipeline.revisions import PipelineRevisionRepository, build_migrated_definition
from mail_verdict.pipeline.runner import QUEUE_NAME, PipelineRunner
from mail_verdict.queue.circuit import CircuitBreaker, CircuitState
from mail_verdict.queue.manager import QueueManager
from mail_verdict.settings.credentials import ProviderCredentialRepository
from mail_verdict.settings.service import SettingsService


async def _seed_account_and_folders(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert an account, an INBOX and a Junk (special_use=junk) folder.

    Returns:
        (account_id, inbox_folder_id, junk_folder_id)
    """
    account_id = uuid.uuid4()
    inbox_id = uuid.uuid4()
    junk_id = uuid.uuid4()

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
        {"id": inbox_id, "account_id": account_id},
    )
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use) "
            "VALUES (:id, :account_id, 'Junk', 'junk')"
        ),
        {"id": junk_id, "account_id": account_id},
    )
    return account_id, inbox_id, junk_id


async def _seed_message(
    session: AsyncSession, *, account_id: uuid.UUID, folder_id: uuid.UUID,
    subject: str = "Cheap viagra offer", from_addr: str = "sender@example.com",
) -> tuple[uuid.UUID, str]:
    """Insert a message; returns (mail_id, message_id_hdr)."""
    mail_id = uuid.uuid4()
    header = f"<{uuid.uuid4()}@example.com>"
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, from_addr, "
            "subject, body_text, received_at, size_bytes, is_seen) "
            "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :message_id, :from_addr, "
            ":subject, :body_text, :received_at, 1024, false)"
        ),
        {
            "id": mail_id, "account_id": account_id, "folder_id": folder_id,
            "thread_id": uuid.uuid4(), "message_id": header, "from_addr": from_addr,
            "subject": subject, "body_text": "Buy now, cheap viagra, no prescription needed.",
            "received_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
    )
    return mail_id, header


async def _make_runner(db: DatabaseConnection) -> PipelineRunner:
    """A PipelineRunner wired against fake classification -- no network
    call, no real API key, deterministic on the seeded subject/body.

    Appends a fresh copy of the default classify + move-spam definition
    as the current revision before returning -- see the module docstring
    for why this cannot simply trust migrated_db's existing state."""
    document = build_migrated_definition(
        raw_rules=[],
        spam_settings={"enabled": True, "auto_move_to_junk": True, "auto_mark_read": True},
    )
    await PipelineRevisionRepository(db).append(document, note="test baseline")

    settings_service = SettingsService(db)
    await settings_service.load()
    await settings_service.update("ai", {"provider": "fake"})

    cred_repo = ProviderCredentialRepository(db, encryption_key="")
    account_prefs_repo = AccountPrefsRepository(db)
    return PipelineRunner(db, settings_service, cred_repo, account_prefs_repo, event_ring=None)


async def _enqueue_and_run(
    runner: PipelineRunner, db: DatabaseConnection,
    *, account_id: uuid.UUID, mail_id: uuid.UUID, msg_key: str, origin: str = "live",
) -> dict[str, object]:
    """Insert a pipeline_runs row and drive it through claim + handle,
    bypassing postimap_events (pipeline/enqueue.py is exercised
    separately) -- this is the runner's own contract under test."""
    async with db.session() as session:
        result = await session.execute(
            text(
                "INSERT INTO pipeline_runs (account_id, msg_key, message_id, dedup_key, origin) "
                "VALUES (:account_id, :msg_key, :message_id, 'live', :origin) RETURNING id"
            ),
            {"account_id": account_id, "msg_key": msg_key, "message_id": mail_id, "origin": origin},
        )
        run_id = result.scalar_one()

    claimed = await runner._work_queue.claim_batch(worker_id="test", batch_size=1, lease_seconds=30)
    assert len(claimed) == 1
    assert claimed[0]["id"] == run_id
    await runner._handle_item(claimed[0])
    return dict(claimed[0])


@pytest.mark.asyncio
async def test_live_message_is_classified_and_moved_to_junk(
    migrated_db: DatabaseConnection,
) -> None:
    """The default migrated definition (classify, then move-spam) runs
    end to end: a verdict is recorded and the message lands in Junk, seen."""
    async with migrated_db.session() as session:
        account_id, inbox_id, junk_id = await _seed_account_and_folders(session)
        mail_id, header = await _seed_message(session, account_id=account_id, folder_id=inbox_id)
        await session.commit()

    account_prefs_repo = AccountPrefsRepository(migrated_db)
    await account_prefs_repo.update(account_id, spam_enabled=True)

    runner = await _make_runner(migrated_db)
    await _enqueue_and_run(
        runner, migrated_db, account_id=account_id, mail_id=mail_id, msg_key=header,
    )

    async with migrated_db.session() as session:
        run_row = (
            await session.execute(
                text("SELECT status, halted_at_stage FROM pipeline_runs WHERE message_id = :id"),
                {"id": mail_id},
            )
        ).one()
        msg_row = (
            await session.execute(
                text("SELECT folder_id, is_seen FROM messages WHERE id = :id"), {"id": mail_id},
            )
        ).one()

    assert run_row.status == "done"
    assert msg_row.folder_id == junk_id
    assert msg_row.is_seen is True

    verdict_repo = VerdictRepository(migrated_db)
    verdict = await verdict_repo.get_latest_for_mail(mail_id)
    assert verdict is not None
    assert verdict.is_spam is True
    assert verdict.source.value == "ai"


@pytest.mark.asyncio
async def test_a_message_is_never_classified_twice(migrated_db: DatabaseConnection) -> None:
    """Running the classify stage again for the same message identity
    produces no second verdict -- the never-classify-twice gate holds
    even when the same run is (hypothetically) executed more than once."""
    async with migrated_db.session() as session:
        account_id, inbox_id, junk_id = await _seed_account_and_folders(session)
        mail_id, header = await _seed_message(session, account_id=account_id, folder_id=inbox_id)
        await session.commit()

    await AccountPrefsRepository(migrated_db).update(account_id, spam_enabled=True)
    runner = await _make_runner(migrated_db)

    row = await _enqueue_and_run(
        runner, migrated_db, account_id=account_id, mail_id=mail_id, msg_key=header,
    )
    # A second execution of the very same claimed row -- idempotency, not
    # queue mechanics (the row is already 'done'; this calls the runner
    # directly the way a crash-and-re-execute would).
    await runner._execute_run(row)

    async with migrated_db.session() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM verdicts WHERE mail_id = :id AND source = 'ai'"),
                {"id": mail_id},
            )
        ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_classify_never_runs_on_historical_origin(migrated_db: DatabaseConnection) -> None:
    """A run with origin='historical' reaches the classify stage and is
    structurally ineligible -- runs_on excludes it -- so a mailbox backfill
    can never produce a verdict, whatever the message's content."""
    async with migrated_db.session() as session:
        account_id, inbox_id, _junk_id = await _seed_account_and_folders(session)
        mail_id, header = await _seed_message(session, account_id=account_id, folder_id=inbox_id)
        await session.commit()

    await AccountPrefsRepository(migrated_db).update(account_id, spam_enabled=True)
    runner = await _make_runner(migrated_db)

    await _enqueue_and_run(
        runner, migrated_db, account_id=account_id, mail_id=mail_id, msg_key=header,
        origin="historical",
    )

    async with migrated_db.session() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM verdicts WHERE mail_id = :id"), {"id": mail_id},
            )
        ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_expunged_message_is_skipped_not_classified(migrated_db: DatabaseConnection) -> None:
    """A message expunged before its run executes is never classified --
    the scope re-check at execution time, not just at enqueue."""
    async with migrated_db.session() as session:
        account_id, inbox_id, _junk_id = await _seed_account_and_folders(session)
        mail_id, header = await _seed_message(session, account_id=account_id, folder_id=inbox_id)
        await session.execute(
            text("UPDATE messages SET expunged_at = now() WHERE id = :id"), {"id": mail_id},
        )
        await session.commit()

    await AccountPrefsRepository(migrated_db).update(account_id, spam_enabled=True)
    runner = await _make_runner(migrated_db)

    row = await _enqueue_and_run(
        runner, migrated_db, account_id=account_id, mail_id=mail_id, msg_key=header,
    )

    async with migrated_db.session() as session:
        run_row = (
            await session.execute(
                text("SELECT status, skip_reason FROM pipeline_runs WHERE id = :id"),
                {"id": row["id"]},
            )
        ).one()
    assert run_row.status == "skipped"
    assert run_row.skip_reason == "message gone"


@pytest.mark.asyncio
async def test_the_queue_reports_the_breaker_a_classify_call_actually_trips(
    migrated_db: DatabaseConnection,
) -> None:
    """ModelGateway names its breaker for the provider, so the queue's
    reported circuit has to follow `ai.provider` rather than the queue's
    own name -- otherwise the readout shows a breaker nothing writes to
    while the real one goes unseen."""
    runner = await _make_runner(migrated_db)
    manager = QueueManager(migrated_db)
    runner.register(manager)

    await CircuitBreaker(migrated_db, "anthropic").record_unavailable(
        reason="no key configured", probe_interval=timedelta(minutes=5),
    )

    assert (await manager.summary(QUEUE_NAME)).circuit.state == CircuitState.CLOSED
    await runner._settings.update("ai", {"provider": "anthropic"})
    assert (await manager.summary(QUEUE_NAME)).circuit.state == CircuitState.SUSPENDED


@pytest.mark.asyncio
async def test_a_row_at_max_attempts_is_never_reclaimed_by_the_worker_loop(
    migrated_db: DatabaseConnection,
) -> None:
    """A row that crashes the worker process itself, every time, before
    ever reaching _retry_transient's own cap is only stopped by
    max_attempts reaching claim_batch -- proving _worker_body actually
    passes the pipeline's own setting through to default_worker_loop,
    not just that claim_batch honours the argument when given one."""
    runner = await _make_runner(migrated_db)
    await runner._settings.update(
        "pipeline", {"poll_interval_seconds": 0.05, "max_attempts": 2},
    )

    async with migrated_db.session() as session:
        account_id, inbox_id, _junk_id = await _seed_account_and_folders(session)
        mail_id, header = await _seed_message(session, account_id=account_id, folder_id=inbox_id)

    async with migrated_db.session() as session:
        result = await session.execute(
            text(
                "INSERT INTO pipeline_runs "
                "(account_id, msg_key, message_id, dedup_key, origin, status, attempts) "
                "VALUES (:account_id, :msg_key, :message_id, 'live', 'live', 'pending', 2) "
                "RETURNING id"
            ),
            {"account_id": account_id, "msg_key": header, "message_id": mail_id},
        )
        run_id = result.scalar_one()

    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(runner._worker_body("test-cap-worker", stop_event))
    await asyncio.sleep(0.3)
    stop_event.set()
    await asyncio.wait_for(worker_task, timeout=5)

    async with migrated_db.session() as session:
        row = (
            await session.execute(
                text("SELECT status, attempts, claimed_by FROM pipeline_runs WHERE id = :id"),
                {"id": run_id},
            )
        ).one()
    assert row.status == "pending"
    assert row.attempts == 2
    assert row.claimed_by is None
