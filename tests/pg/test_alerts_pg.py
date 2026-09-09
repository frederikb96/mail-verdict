"""
The alerts table's own repository, and turning a live mail arrival into
one -- the fires-exactly-once dedupe gate, the durable list, the unseen
count, dismissal, and the staged-then-finalized path a message that can
still be refiled by the pipeline goes through -- against a real Postgres
schema.
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from mail_verdict.alerts.dispatch import (
    _finalize_pending_mail_alerts_once,
    create_mail_alert_for_arrival,
)
from mail_verdict.api.event_ring import EventRing
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Alert
from mail_verdict.database.repository import AlertRepository, PushSubscriptionRepository
from mail_verdict.postimap.actions import move_message
from mail_verdict.push.vapid import VapidKeyRepository
from mail_verdict.settings.service import SettingsService
from tests.pg.test_bulk_actions_and_outbox import _seed_account_two_folders

_imap_uid_counter = itertools.count(1)


async def _settings(db: DatabaseConnection, **mail_overrides: object) -> SettingsService:
    service = SettingsService(db)
    await service.load()
    if mail_overrides:
        await service.update("mail", mail_overrides)
    return service


async def _seed_message(
    session, account_id: uuid.UUID, folder_id: uuid.UUID, *,
    subject: str = "Hello", from_addr: str = "sender@example.com",
) -> uuid.UUID:
    """received_at is set explicitly -- unlike created_at, it carries no
    server default (see the consumer contract), and
    is_live_pipeline_possible's age-limit check reads NULL as excluded
    rather than as "unknown, assume recent"."""
    message_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, from_addr, "
            "received_at) "
            "VALUES (:id, :account_id, :folder_id, :uid, :thread_id, :msg_id, :subject, "
            ":from_addr, now())"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": folder_id,
            "uid": next(_imap_uid_counter),
            "thread_id": uuid.uuid4(), "msg_id": f"<{message_id}@example.com>",
            "subject": subject, "from_addr": from_addr,
        },
    )
    return message_id


async def _seed_archive_folder(session, account_id: uuid.UUID) -> uuid.UUID:
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use) "
            "VALUES (:id, :account_id, 'Archive', 'archive')"
        ),
        {"id": folder_id, "account_id": account_id},
    )
    return folder_id


async def _seed_plain_folder(session, account_id: uuid.UUID, imap_name: str) -> uuid.UUID:
    """A second ordinary folder, special_use unset -- still pipeline-
    eligible, unlike Junk/Archive/Trash/Sent/Drafts, so moving a message
    here isolates "pipeline run reached a terminal status" from "the
    folder itself makes the pipeline dead" as the reason a staged alert
    gets delivered."""
    folder_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO folders (id, account_id, imap_name) VALUES (:id, :account_id, :name)"),
        {"id": folder_id, "account_id": account_id, "name": imap_name},
    )
    return folder_id


async def _seed_watermark(session, *, account_id: uuid.UUID, folder_id: uuid.UUID) -> None:
    """A folder's pipeline watermark -- the same signal
    pipeline/enqueue.py's record_folder_watermark writes once PostIMAP
    reports that folder's first sync complete. Mail arriving in a folder
    with no such row is never live-pipeline-eligible, whatever its role.

    Backdated by a minute rather than set to bare now(): within one
    uncommitted transaction Postgres's now() is the transaction's start
    time, not the wall clock, so a watermark and a message seeded in the
    same session/transaction (as every caller here does) would otherwise
    tie -- and is_live_pipeline_possible requires the message strictly
    newer than the watermark."""
    await session.execute(
        text(
            "INSERT INTO pipeline_folder_state (folder_id, account_id, backfill_completed_at) "
            "VALUES (:folder_id, :account_id, now() - interval '1 minute')"
        ),
        {"folder_id": folder_id, "account_id": account_id},
    )


async def _seed_account_two_folders_with_watermark(
    session,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """_seed_account_two_folders, plus a pipeline watermark on the inbox
    -- what every test below expecting an ordinary-folder arrival to
    stage (rather than deliver immediately) needs, since
    is_live_pipeline_possible checks for one rather than only the
    folder's role."""
    account_id, inbox_id, junk_id = await _seed_account_two_folders(session)
    await _seed_watermark(session, account_id=account_id, folder_id=inbox_id)
    return account_id, inbox_id, junk_id


@pytest_asyncio.fixture(autouse=True)
async def _drop_seeded_pipeline_runs(migrated_db: DatabaseConnection):
    """Remove this module's seeded pipeline_runs rows after every test.

    The whole pg layer shares one database for the invocation, and the rows
    seeded below are deliberately left in a non-terminal status -- which is
    exactly what the work queue considers claimable. A later module that
    inserts a run and claims one row back assumes the queue holds nothing
    else, so leftovers here surface there as a run id mismatch: each test
    claims the previous leaker's row, and the failure names the wrong file
    entirely.
    """
    yield
    async with migrated_db.session() as session:
        await session.execute(text("DELETE FROM pipeline_runs WHERE dedup_key = 'live'"))
        await session.commit()


async def _seed_pipeline_run(
    session, *, account_id: uuid.UUID, message_id: uuid.UUID, status: str,
) -> None:
    """A pipeline_runs row in a given status -- 'pending'/'claimed' are
    not terminal, everything else (done/failed/skipped) is."""
    await session.execute(
        text(
            "INSERT INTO pipeline_runs "
            "(account_id, msg_key, message_id, dedup_key, origin, apply, status) "
            "VALUES (:account_id, :msg_key, :message_id, 'live', 'live', true, :status)"
        ),
        {
            "account_id": account_id, "msg_key": f"key-{uuid.uuid4()}",
            "message_id": message_id, "status": status,
        },
    )


async def _alert_row(db: DatabaseConnection, message_id: uuid.UUID) -> Alert | None:
    """Read the alert row directly, delivered or still staged -- unlike
    AlertRepository.list_recent/unseen_count, which only ever see a
    delivered row."""
    async with db.session() as session:
        result = await session.execute(select(Alert).where(Alert.message_id == message_id))
        return result.scalars().first()


class TestCreateMailAlert:
    """The dedupe gate itself: a second insert sharing dedupe_key is a
    no-op, not a second row -- what makes a resync safe to re-run."""

    @pytest.mark.asyncio
    async def test_first_insert_returns_the_row_second_is_a_no_op(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            message_id = await _seed_message(session, account_id, inbox_id)
            await session.commit()

        repo = AlertRepository(migrated_db)
        first = await repo.create_mail_alert(
            account_id=account_id, message_id=message_id, msg_key="msg-key-1",
            title="Hello", body="sender@example.com",
        )
        assert first is not None
        assert first.kind == "mail"
        assert first.title == "Hello"
        assert first.url == f"/?message={message_id}"
        assert first.delivered_at is not None

        second = await repo.create_mail_alert(
            account_id=account_id, message_id=message_id, msg_key="msg-key-1",
            title="Hello again", body="different",
        )
        assert second is None

        # list_recent is deliberately not account-scoped (an installed
        # application watches every account from one bell), so a shared
        # test database accumulates rows across tests -- filter to this
        # test's own message_id rather than asserting the whole list.
        matching = [a for a in await repo.list_recent() if a.message_id == message_id]
        assert len(matching) == 1

    @pytest.mark.asyncio
    async def test_staged_row_is_invisible_until_delivered(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            message_id = await _seed_message(session, account_id, inbox_id)
            await session.commit()

        repo = AlertRepository(migrated_db)
        staged = await repo.create_mail_alert(
            account_id=account_id, message_id=message_id, msg_key=f"staged-{uuid.uuid4()}",
            title="Later", body=None, delivered=False,
        )
        assert staged is not None
        assert staged.delivered_at is None

        matching = [a for a in await repo.list_recent() if a.message_id == message_id]
        assert matching == []


class TestImmediateDelivery:
    """A message no pipeline run will ever exist for -- arriving directly
    into a folder the pipeline never runs against (here, Junk), a folder
    with no watermark, or mail older than pipeline.live_max_age_days --
    is delivered the moment it arrives -- no terminal pipeline status
    will ever tell finalize_pending_mail_alerts_once to stop waiting."""

    @pytest.mark.asyncio
    async def test_inserts_one_delivered_alert_with_subject_and_sender(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, _inbox_id, junk_id = await _seed_account_two_folders(session)
            message_id = await _seed_message(
                session, account_id, junk_id,
                subject="Quarterly report", from_addr="finance@example.com",
            )
            await session.commit()

        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=junk_id,
        )

        alert = await _alert_row(migrated_db, message_id)
        assert alert is not None
        assert alert.delivered_at is not None
        assert alert.title == "Quarterly report"
        assert alert.body == "finance@example.com"
        assert alert.folder_id == junk_id

    @pytest.mark.asyncio
    async def test_a_resync_never_produces_a_second_alert(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, _inbox_id, junk_id = await _seed_account_two_folders(session)
            message_id = await _seed_message(session, account_id, junk_id)
            await session.commit()

        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=junk_id,
        )
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=junk_id,
        )

        repo = AlertRepository(migrated_db)
        matching = [a for a in await repo.list_recent() if a.message_id == message_id]
        assert len(matching) == 1

    @pytest.mark.asyncio
    async def test_pushes_alert_new_over_the_event_ring(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, _inbox_id, junk_id = await _seed_account_two_folders(session)
            message_id = await _seed_message(
                session, account_id, junk_id, subject="Ping", from_addr="a@example.com",
            )
            await session.commit()

        # replay_from(0, ...) reads as "gap too large" on a ring whose
        # oldest retained id is 1 -- a harmless seed event gives this
        # account a real oldest id to measure a baseline against, the
        # same idiom test_effects_events.py already uses.
        ring = EventRing()
        await ring.add(account_id, "test.seed", {})
        seq_before = ring.get_latest_seq()
        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, ring, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=junk_id,
        )

        events = await ring.replay_from(seq_before, str(account_id))
        alert_events = [e for e in events if e["event_type"] == "alert.new"]
        assert len(alert_events) == 1
        assert alert_events[0]["data"]["title"] == "Ping"
        assert alert_events[0]["data"]["url"] == f"/?message={message_id}"
        assert alert_events[0]["data"]["folder_id"] == str(junk_id)

    @pytest.mark.asyncio
    async def test_a_message_already_expunged_is_skipped(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Not found -- expunged and purged between the postimap insert
        event firing and this call reaching the database -- must not
        raise; there is nothing left to build an alert about."""
        async with migrated_db.session() as session:
            account_id, _inbox_id, junk_id = await _seed_account_two_folders(session)
            await session.commit()

        dead_message_id = uuid.uuid4()
        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=dead_message_id,
            settings_service=settings_service, folder_id=junk_id,
        )

        assert await _alert_row(migrated_db, dead_message_id) is None

    @pytest.mark.asyncio
    async def test_a_folder_with_no_watermark_delivers_immediately(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """An otherwise ordinary folder that has never gotten a
        pipeline_folder_state row -- the same permanent state a folder
        synced before the pipeline feature shipped is left in -- can
        never produce a pipeline_runs row either, so staging and waiting
        out the bound would only ever end in the bound. Delivered right
        away instead, the same as an explicitly excluded folder."""
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            message_id = await _seed_message(session, account_id, inbox_id, subject="No watermark")
            await session.commit()

        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=inbox_id,
        )

        alert = await _alert_row(migrated_db, message_id)
        assert alert is not None
        assert alert.delivered_at is not None
        assert alert.folder_id == inbox_id

    @pytest.mark.asyncio
    async def test_mail_older_than_the_live_max_age_delivers_immediately(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """A watermark exists, but the message's own received_at is older
        than pipeline.live_max_age_days (a backdated Date header, most
        likely) -- exactly as permanently pipeline-ineligible as a
        missing watermark, so this must not wait out the bound either.

        received_at is backdated on the row itself, deliberately, rather
        than by lowering the pipeline.live_max_age_days setting: settings
        persist in the database this whole pg-layer session shares, so a
        lowered setting would silently outlive this test and misclassify
        every later test's own "recent" mail as too old."""
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders_with_watermark(
                session,
            )
            message_id = await _seed_message(session, account_id, inbox_id, subject="Too old")
            await session.execute(
                text("UPDATE messages SET received_at = now() - interval '30 days' WHERE id = :id"),
                {"id": message_id},
            )
            await session.commit()

        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=inbox_id,
        )

        alert = await _alert_row(migrated_db, message_id)
        assert alert is not None
        assert alert.delivered_at is not None
        assert alert.folder_id == inbox_id


class TestStagedArrival:
    """A message arriving in an ordinary folder the pipeline can still act
    on is staged, not delivered -- the fix for a notification announcing
    the arrival folder rather than wherever a rule later files the
    message."""

    @pytest.mark.asyncio
    async def test_arriving_in_an_ordinary_folder_is_staged_not_delivered(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders_with_watermark(
                session,
            )
            message_id = await _seed_message(session, account_id, inbox_id, subject="Staged")
            await session.commit()

        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=inbox_id,
        )

        alert = await _alert_row(migrated_db, message_id)
        assert alert is not None
        assert alert.delivered_at is None
        assert alert.title == "Staged"
        # The arrival folder is stored as a placeholder even while staged.
        assert alert.folder_id == inbox_id

        repo = AlertRepository(migrated_db)
        matching = [a for a in await repo.list_recent() if a.message_id == message_id]
        assert matching == []

    @pytest.mark.asyncio
    async def test_staged_arrival_pushes_nothing_yet(self, migrated_db: DatabaseConnection) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders_with_watermark(
                session,
            )
            message_id = await _seed_message(session, account_id, inbox_id)
            await session.commit()

        ring = EventRing()
        await ring.add(account_id, "test.seed", {})
        seq_before = ring.get_latest_seq()
        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, ring, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=inbox_id,
        )

        events = await ring.replay_from(seq_before, str(account_id))
        assert [e for e in events if e["event_type"] == "alert.new"] == []

    @pytest.mark.asyncio
    async def test_a_resync_of_a_staged_arrival_never_produces_a_second_row(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders_with_watermark(
                session,
            )
            message_id = await _seed_message(session, account_id, inbox_id)
            await session.commit()

        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=inbox_id,
        )
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=inbox_id,
        )

        async with migrated_db.session() as session:
            result = await session.execute(select(Alert).where(Alert.message_id == message_id))
            assert len(result.scalars().all()) == 1


class TestFinalizePendingMailAlerts:
    """The periodic pass: a staged alert is delivered once its message's
    pipeline run reaches a terminal status, once its message can no
    longer reach one, or once the bound expires -- always with the
    folder the message is actually in by then."""

    @pytest.mark.asyncio
    async def test_terminal_pipeline_run_delivers_with_the_current_folder(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders_with_watermark(
                session,
            )
            # A second ordinary folder, not Junk/Archive/Trash -- so only
            # the terminal pipeline run, never the folder itself, can
            # explain the alert being delivered below.
            other_id = await _seed_plain_folder(session, account_id, "Filed")
            message_id = await _seed_message(session, account_id, inbox_id, subject="Filed")
            await session.commit()

        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=inbox_id,
        )
        staged = await _alert_row(migrated_db, message_id)
        assert staged is not None
        assert staged.delivered_at is None

        # A rule moved it out of the inbox, and its pipeline run finished,
        # before the alert was ever delivered.
        async with migrated_db.session() as session:
            await move_message(session, message_id, other_id)
            await _seed_pipeline_run(
                session, account_id=account_id, message_id=message_id, status="done",
            )
            await session.commit()

        settings_service = await _settings(migrated_db)
        ring = EventRing()
        await _finalize_pending_mail_alerts_once(migrated_db, ring, None, settings_service)

        alert = await _alert_row(migrated_db, message_id)
        assert alert is not None
        assert alert.delivered_at is not None
        assert alert.folder_id == other_id

    @pytest.mark.asyncio
    async def test_pending_pipeline_run_within_bound_stays_staged(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders_with_watermark(
                session,
            )
            message_id = await _seed_message(session, account_id, inbox_id)
            await session.commit()

        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=inbox_id,
        )
        async with migrated_db.session() as session:
            await _seed_pipeline_run(
                session, account_id=account_id, message_id=message_id, status="pending",
            )
            await session.commit()

        settings_service = await _settings(migrated_db, notify_wait_seconds=3600.0)
        await _finalize_pending_mail_alerts_once(migrated_db, None, None, settings_service)

        alert = await _alert_row(migrated_db, message_id)
        assert alert is not None
        assert alert.delivered_at is None

    @pytest.mark.asyncio
    async def test_bound_expiring_delivers_with_whatever_folder_it_is_in(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """No pipeline_runs row at all (a stalled embeddings provider,
        say, so the message's own transition to a terminal pipeline
        status never happens) is exactly the case the bound exists for --
        distinct from a folder with no watermark, which is now caught at
        arrival time instead (see TestImmediateDelivery)."""
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders_with_watermark(
                session,
            )
            message_id = await _seed_message(session, account_id, inbox_id)
            await session.commit()

        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=inbox_id,
        )

        settings_service = await _settings(migrated_db, notify_wait_seconds=0.0)
        await _finalize_pending_mail_alerts_once(migrated_db, None, None, settings_service)

        alert = await _alert_row(migrated_db, message_id)
        assert alert is not None
        assert alert.delivered_at is not None
        assert alert.folder_id == inbox_id

    @pytest.mark.asyncio
    async def test_moved_to_a_pipeline_excluded_folder_delivers_before_the_bound(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """A message the user or an unrelated action moved to Archive
        before its own pipeline run ever reached one is exactly as
        pipeline-dead as one that arrived there -- delivered on the very
        next tick rather than waiting out the full bound."""
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders_with_watermark(
                session,
            )
            archive_id = await _seed_archive_folder(session, account_id)
            message_id = await _seed_message(session, account_id, inbox_id)
            await session.commit()

        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=inbox_id,
        )
        async with migrated_db.session() as session:
            await move_message(session, message_id, archive_id)
            await session.commit()

        settings_service = await _settings(migrated_db, notify_wait_seconds=3600.0)
        await _finalize_pending_mail_alerts_once(migrated_db, None, None, settings_service)

        alert = await _alert_row(migrated_db, message_id)
        assert alert is not None
        assert alert.delivered_at is not None
        assert alert.folder_id == archive_id

    @pytest.mark.asyncio
    async def test_expunged_before_delivery_is_dropped(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders_with_watermark(
                session,
            )
            message_id = await _seed_message(session, account_id, inbox_id)
            await session.commit()

        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=inbox_id,
        )
        async with migrated_db.session() as session:
            await session.execute(
                text("UPDATE messages SET expunged_at = now() WHERE id = :id"),
                {"id": message_id},
            )
            await session.commit()

        settings_service = await _settings(migrated_db, notify_wait_seconds=0.0)
        await _finalize_pending_mail_alerts_once(migrated_db, None, None, settings_service)

        assert await _alert_row(migrated_db, message_id) is None

    @pytest.mark.asyncio
    async def test_finalizing_pushes_alert_new_with_the_current_folder(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, junk_id = await _seed_account_two_folders_with_watermark(
                session,
            )
            message_id = await _seed_message(session, account_id, inbox_id, subject="Filed")
            await session.commit()

        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=inbox_id,
        )
        async with migrated_db.session() as session:
            await move_message(session, message_id, junk_id)
            await session.commit()

        ring = EventRing()
        await ring.add(account_id, "test.seed", {})
        seq_before = ring.get_latest_seq()
        settings_service = await _settings(migrated_db, notify_wait_seconds=0.0)
        await _finalize_pending_mail_alerts_once(migrated_db, ring, None, settings_service)

        events = await ring.replay_from(seq_before, str(account_id))
        alert_events = [e for e in events if e["event_type"] == "alert.new"]
        assert len(alert_events) == 1
        assert alert_events[0]["data"]["folder_id"] == str(junk_id)

    @pytest.mark.asyncio
    async def test_a_second_finalize_pass_never_redelivers(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders_with_watermark(
                session,
            )
            message_id = await _seed_message(session, account_id, inbox_id)
            await session.commit()

        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=inbox_id,
        )
        settings_service = await _settings(migrated_db, notify_wait_seconds=0.0)
        ring = EventRing()
        await _finalize_pending_mail_alerts_once(migrated_db, ring, None, settings_service)

        await ring.add(account_id, "test.seed", {})
        seq_before = ring.get_latest_seq()
        await _finalize_pending_mail_alerts_once(migrated_db, ring, None, settings_service)

        events = await ring.replay_from(seq_before, str(account_id))
        assert [e for e in events if e["event_type"] == "alert.new"] == []

    @pytest.mark.asyncio
    async def test_finalizing_triggers_a_background_push_dispatch(
        self, migrated_db: DatabaseConnection, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Not push/send.py's own coverage (test_push_pg.py has that) --
        this proves the wiring: a delivered-by-finalize alert reaches a
        push attempt exactly as one delivered immediately would, as the
        fire-and-forget background task _deliver() does not itself await.

        The message stays in the inbox throughout -- a subscription with
        no explicit alert_folder_ids only matches an arrival folder (see
        PushSubscriptionRepository.list_for_alert), and every folder a
        staged alert can be finalized from is by definition one the
        pipeline still runs against, never Junk or Archive."""
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders_with_watermark(
                session,
            )
            message_id = await _seed_message(session, account_id, inbox_id, subject="Push me")
            await session.commit()

        push_repo = PushSubscriptionRepository(migrated_db)
        sub = await push_repo.upsert(
            endpoint=f"https://push.example/{uuid.uuid4()}", p256dh="p", auth="a", label=None,
        )

        called_endpoints: list[str] = []

        async def fake_webpush(**kwargs: object) -> None:
            called_endpoints.append(kwargs["subscription_info"]["endpoint"])  # type: ignore[index]

        monkeypatch.setattr(
            "mail_verdict.push.send.webpush_async", AsyncMock(side_effect=fake_webpush),
        )

        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=inbox_id,
        )

        vapid_repo = VapidKeyRepository(migrated_db, "00" * 32)
        settings_service = await _settings(migrated_db, notify_wait_seconds=0.0)
        started = {t for t in asyncio.all_tasks()}
        await _finalize_pending_mail_alerts_once(migrated_db, None, vapid_repo, settings_service)
        # The push dispatch is a fire-and-forget task _deliver() does not
        # itself await -- give it a chance to actually run before
        # asserting anything about it.
        spawned = [t for t in asyncio.all_tasks() - started]
        if spawned:
            await asyncio.gather(*spawned)

        assert sub.endpoint in called_endpoints


class TestListAndDismiss:
    @pytest.mark.asyncio
    async def test_unseen_count_and_dismiss(self, migrated_db: DatabaseConnection) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            first = await _seed_message(session, account_id, inbox_id, subject="One")
            second = await _seed_message(session, account_id, inbox_id, subject="Two")
            await session.commit()

        # unseen_count is deliberately not account-scoped (see
        # list_recent's own comment) and a shared test database
        # accumulates undismissed rows across every other test in this
        # file -- measure the delta this test itself produces, never the
        # absolute count.
        repo = AlertRepository(migrated_db)
        baseline = await repo.unseen_count()
        alert_one = await repo.create_mail_alert(
            account_id=account_id, message_id=first, msg_key="k1", title="One", body=None,
        )
        await repo.create_mail_alert(
            account_id=account_id, message_id=second, msg_key="k2", title="Two", body=None,
        )
        assert alert_one is not None

        assert await repo.unseen_count() - baseline == 2

        dismissed = await repo.dismiss(alert_one.id)
        assert dismissed is True
        assert await repo.unseen_count() - baseline == 1

        # Idempotent -- dismissing an already-dismissed alert is a no-op,
        # not an error.
        assert await repo.dismiss(alert_one.id) is False

        after_dismiss_all = await repo.unseen_count()
        remaining = await repo.dismiss_all()
        assert remaining == after_dismiss_all
        assert await repo.unseen_count() == 0

    @pytest.mark.asyncio
    async def test_list_recent_unseen_only_excludes_dismissed(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """unseen_only is what a caller needing its list and its count to
        agree (the bell badge) asks for -- a dismissed alert must not
        reappear in it even though it's still within the recency window."""
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            seen_message = await _seed_message(session, account_id, inbox_id, subject="Seen")
            unseen_message = await _seed_message(session, account_id, inbox_id, subject="Unseen")
            await session.commit()

        repo = AlertRepository(migrated_db)
        seen_alert = await repo.create_mail_alert(
            account_id=account_id, message_id=seen_message, msg_key=f"unseen-only-{uuid.uuid4()}",
            title="Seen", body=None,
        )
        await repo.create_mail_alert(
            account_id=account_id, message_id=unseen_message,
            msg_key=f"unseen-only-{uuid.uuid4()}", title="Unseen", body=None,
        )
        assert seen_alert is not None
        await repo.dismiss(seen_alert.id)

        unseen_only = await repo.list_recent(limit=200, unseen_only=True)
        ids = {a.message_id for a in unseen_only}
        assert unseen_message in ids
        assert seen_message not in ids

    @pytest.mark.asyncio
    async def test_list_recent_is_newest_first_and_respects_limit(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            ids = [
                await _seed_message(session, account_id, inbox_id, subject=f"m{i}")
                for i in range(3)
            ]
            await session.commit()

        repo = AlertRepository(migrated_db)
        for i, message_id in enumerate(ids):
            await repo.create_mail_alert(
                account_id=account_id, message_id=message_id, msg_key=f"key-{i}",
                title=f"m{i}", body=None,
            )

        recent = await repo.list_recent(limit=2)
        assert len(recent) == 2
        # created_at ties (all inserted in the same test, possibly the
        # same microsecond) are broken by id descending -- assert on the
        # count and the shape rather than a specific order that depends
        # on wall-clock resolution.
        assert all(a.kind == "mail" for a in recent)


class TestFolderScope:
    """create_mail_alert_for_arrival threads folder_id onto the row
    itself, and list_recent/unseen_count read it back -- the same "which
    folders alert" preference the SSE and push paths already compute, now
    applying to the durable list and badge too rather than only to a live
    notification."""

    @pytest.mark.asyncio
    async def test_create_mail_alert_for_arrival_stores_the_folder(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, _inbox_id, junk_id = await _seed_account_two_folders(session)
            message_id = await _seed_message(session, account_id, junk_id)
            await session.commit()

        settings_service = await _settings(migrated_db)
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
            settings_service=settings_service, folder_id=junk_id,
        )

        repo = AlertRepository(migrated_db)
        matching = [a for a in await repo.list_recent() if a.message_id == message_id]
        assert len(matching) == 1
        assert matching[0].folder_id == junk_id

    @pytest.mark.asyncio
    async def test_list_recent_and_unseen_count_honour_a_folder_filter(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, junk_id = await _seed_account_two_folders(session)
            inbox_message = await _seed_message(session, account_id, inbox_id, subject="Inbox")
            junk_message = await _seed_message(session, account_id, junk_id, subject="Junk")
            await session.commit()

        repo = AlertRepository(migrated_db)
        baseline = await repo.unseen_count(folder_ids=[inbox_id])
        await repo.create_mail_alert(
            account_id=account_id, message_id=inbox_message, msg_key=f"scope-{uuid.uuid4()}",
            title="Inbox", body=None, folder_id=inbox_id,
        )
        await repo.create_mail_alert(
            account_id=account_id, message_id=junk_message, msg_key=f"scope-{uuid.uuid4()}",
            title="Junk", body=None, folder_id=junk_id,
        )

        scoped_to_inbox = await repo.list_recent(folder_ids=[inbox_id])
        assert inbox_message in {a.message_id for a in scoped_to_inbox}
        assert junk_message not in {a.message_id for a in scoped_to_inbox}
        assert await repo.unseen_count(folder_ids=[inbox_id]) - baseline == 1

        unrestricted = await repo.list_recent(folder_ids=None)
        assert {inbox_message, junk_message} <= {a.message_id for a in unrestricted}

        scoped_to_nothing = await repo.list_recent(folder_ids=[])
        assert inbox_message not in {a.message_id for a in scoped_to_nothing}
        assert junk_message not in {a.message_id for a in scoped_to_nothing}

    @pytest.mark.asyncio
    async def test_a_row_with_no_folder_passes_every_filter(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """A row that predates the column, or a future reminder kind with
        no folder at all, is never excluded by a folder preference -- the
        preference has nothing to say about it."""
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            message_id = await _seed_message(session, account_id, inbox_id)
            await session.commit()

        repo = AlertRepository(migrated_db)
        await repo.create_mail_alert(
            account_id=account_id, message_id=message_id, msg_key=f"nofolder-{uuid.uuid4()}",
            title="No folder", body=None,
        )

        scoped = await repo.list_recent(folder_ids=[uuid.uuid4()])
        assert message_id in {a.message_id for a in scoped}

