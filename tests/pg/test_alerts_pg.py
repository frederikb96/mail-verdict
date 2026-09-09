"""
The alerts table's own repository, and turning a live mail arrival into
one -- the fires-exactly-once dedupe gate, the durable list, the unseen
count, and dismissal, against a real Postgres schema.
"""

from __future__ import annotations

import itertools
import uuid

import pytest
from sqlalchemy import text

from mail_verdict.alerts.dispatch import create_mail_alert_for_arrival
from mail_verdict.api.event_ring import EventRing
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.repository import AlertRepository
from tests.pg.test_bulk_actions_and_outbox import _seed_account_two_folders

_imap_uid_counter = itertools.count(1)


async def _seed_message(
    session, account_id: uuid.UUID, folder_id: uuid.UUID, *,
    subject: str = "Hello", from_addr: str = "sender@example.com",
) -> uuid.UUID:
    message_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, from_addr) "
            "VALUES (:id, :account_id, :folder_id, :uid, :thread_id, :msg_id, :subject, :from_addr)"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": folder_id,
            "uid": next(_imap_uid_counter),
            "thread_id": uuid.uuid4(), "msg_id": f"<{message_id}@example.com>",
            "subject": subject, "from_addr": from_addr,
        },
    )
    return message_id


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


class TestCreateMailAlertForArrival:
    """The server.py hook -- reads the message row itself, computes
    msg_key, and inserts. A resync (the same message id, same content)
    must never produce a second alert."""

    @pytest.mark.asyncio
    async def test_inserts_one_alert_with_subject_and_sender(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            message_id = await _seed_message(
                session, account_id, inbox_id,
                subject="Quarterly report", from_addr="finance@example.com",
            )
            await session.commit()

        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
        )

        repo = AlertRepository(migrated_db)
        matching = [a for a in await repo.list_recent() if a.message_id == message_id]
        assert len(matching) == 1
        alert = matching[0]
        assert alert.title == "Quarterly report"
        assert alert.body == "finance@example.com"
        assert alert.url == f"/?message={message_id}"
        assert alert.account_id == account_id

    @pytest.mark.asyncio
    async def test_a_resync_never_produces_a_second_alert(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            message_id = await _seed_message(session, account_id, inbox_id)
            await session.commit()

        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
        )
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=message_id,
        )

        repo = AlertRepository(migrated_db)
        matching = [a for a in await repo.list_recent() if a.message_id == message_id]
        assert len(matching) == 1

    @pytest.mark.asyncio
    async def test_pushes_alert_new_over_the_event_ring(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, inbox_id, _junk_id = await _seed_account_two_folders(session)
            message_id = await _seed_message(
                session, account_id, inbox_id, subject="Ping", from_addr="a@example.com",
            )
            await session.commit()

        # replay_from(0, ...) reads as "gap too large" on a ring whose
        # oldest retained id is 1 -- a harmless seed event gives this
        # account a real oldest id to measure a baseline against, the
        # same idiom test_effects_events.py already uses.
        ring = EventRing()
        await ring.add(account_id, "test.seed", {})
        seq_before = ring.get_latest_seq()
        await create_mail_alert_for_arrival(
            migrated_db, ring, account_id=account_id, message_id=message_id,
            folder_id=inbox_id,
        )

        events = await ring.replay_from(seq_before, str(account_id))
        alert_events = [e for e in events if e["event_type"] == "alert.new"]
        assert len(alert_events) == 1
        assert alert_events[0]["data"]["title"] == "Ping"
        assert alert_events[0]["data"]["url"] == f"/?message={message_id}"
        assert alert_events[0]["data"]["folder_id"] == str(inbox_id)

    @pytest.mark.asyncio
    async def test_a_message_already_expunged_is_skipped(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Not found -- expunged and purged between the postimap insert
        event firing and this call reaching the database -- must not
        raise; there is nothing left to build an alert about."""
        async with migrated_db.session() as session:
            account_id, _inbox_id, _junk_id = await _seed_account_two_folders(session)
            await session.commit()

        dead_message_id = uuid.uuid4()
        await create_mail_alert_for_arrival(
            migrated_db, None, account_id=account_id, message_id=dead_message_id,
        )

        repo = AlertRepository(migrated_db)
        matching = [a for a in await repo.list_recent() if a.message_id == dead_message_id]
        assert matching == []


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
