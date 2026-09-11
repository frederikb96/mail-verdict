"""
A message that sits on its way out far longer than it should becomes a
user-visible alert -- once per message, however many passes see it --
rather than waiting silently. Covers both places a message can wait: an
outbox row PostIMAP has not finished with, and a send held in the undo
staging table that never moved on.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from mail_verdict.api.event_ring import EventRing
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Alert
from mail_verdict.outbox.stalled import raise_stalled_outbox_alerts_once

_THRESHOLD = timedelta(minutes=10)


async def _seed_inactive_account(db: DatabaseConnection) -> uuid.UUID:
    """Inactive, so PostIMAP never picks up its outbox and every row seeded
    below keeps exactly the status and age it was given."""
    account_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, "
                "imap_password, is_active) VALUES (:id, :name, 'imap.example.com', 993, "
                "'user@example.com', '\\x00' || convert_to('pw', 'UTF8'), false)"
            ),
            {"id": account_id, "name": f"acct-{account_id}"},
        )
        await session.commit()
    return account_id


async def _seed_outbox_row(
    db: DatabaseConnection, account_id: uuid.UUID, *, kind: str, status: str,
    age: timedelta, subject: str,
) -> uuid.UUID:
    outbox_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO outbox (id, account_id, kind, to_addrs, subject, body_text, "
                "status, created_at) VALUES (:id, :account_id, :kind, '[\"them@example.com\"]', "
                ":subject, 'hi', :status, :created_at)"
            ),
            {
                "id": outbox_id, "account_id": account_id, "kind": kind,
                "subject": subject, "status": status,
                "created_at": datetime.now(timezone.utc) - age,
            },
        )
        await session.commit()
    return outbox_id


async def _seed_pending_send(
    db: DatabaseConnection, account_id: uuid.UUID, *, overdue_by: timedelta, subject: str,
    cancelled: bool = False,
) -> uuid.UUID:
    pending_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO pending_sends (id, account_id, to_addrs, subject, body_text, "
                "send_after, cancelled_at) VALUES (:id, :account_id, '[\"them@example.com\"]', "
                ":subject, 'hi', :send_after, :cancelled_at)"
            ),
            {
                "id": pending_id, "account_id": account_id, "subject": subject,
                "send_after": datetime.now(timezone.utc) - overdue_by,
                "cancelled_at": datetime.now(timezone.utc) if cancelled else None,
            },
        )
        await session.commit()
    return pending_id


async def _alerts_for(db: DatabaseConnection, account_id: uuid.UUID) -> list[Alert]:
    async with db.session() as session:
        result = await session.execute(
            select(Alert).where(Alert.account_id == account_id, Alert.kind == "outbox_stalled")
        )
        return list(result.scalars().all())


class TestStalledOutbox:
    @pytest.mark.asyncio
    async def test_a_send_pending_past_the_threshold_raises_one_alert(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = await _seed_inactive_account(migrated_db)
        await _seed_outbox_row(
            migrated_db, account_id, kind="send", status="pending",
            age=timedelta(hours=1), subject="Stuck send",
        )

        await raise_stalled_outbox_alerts_once(migrated_db, None, None, threshold=_THRESHOLD)

        alerts = await _alerts_for(migrated_db, account_id)
        assert len(alerts) == 1
        assert alerts[0].delivered_at is not None, "an undelivered alert never reaches the bell"
        assert alerts[0].dismissed_at is None
        assert alerts[0].body is not None and "Stuck send" in alerts[0].body

    @pytest.mark.asyncio
    async def test_later_passes_do_not_raise_it_again(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Once, not once per pass -- including after it was dismissed, since
        a dismissed alert is one the user has already seen."""
        account_id = await _seed_inactive_account(migrated_db)
        await _seed_outbox_row(
            migrated_db, account_id, kind="draft", status="processing",
            age=timedelta(hours=1), subject="Stuck draft",
        )

        await raise_stalled_outbox_alerts_once(migrated_db, None, None, threshold=_THRESHOLD)
        async with migrated_db.session() as session:
            await session.execute(
                text("UPDATE alerts SET dismissed_at = now() WHERE account_id = :a"),
                {"a": account_id},
            )
            await session.commit()
        await raise_stalled_outbox_alerts_once(migrated_db, None, None, threshold=_THRESHOLD)
        await raise_stalled_outbox_alerts_once(migrated_db, None, None, threshold=_THRESHOLD)

        assert len(await _alerts_for(migrated_db, account_id)) == 1

    @pytest.mark.asyncio
    async def test_nothing_is_raised_below_the_threshold_or_once_finished(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Only a row still waiting counts. A failed row is being retried and
        a dead one already has PostIMAP's own notification."""
        account_id = await _seed_inactive_account(migrated_db)
        await _seed_outbox_row(
            migrated_db, account_id, kind="send", status="pending",
            age=timedelta(minutes=1), subject="Fresh send",
        )
        for status in ("sent", "failed", "dead"):
            await _seed_outbox_row(
                migrated_db, account_id, kind="send", status=status,
                age=timedelta(hours=1), subject=f"Old {status} send",
            )

        await raise_stalled_outbox_alerts_once(migrated_db, None, None, threshold=_THRESHOLD)

        assert await _alerts_for(migrated_db, account_id) == []

    @pytest.mark.asyncio
    async def test_a_staged_send_that_never_moved_on_raises_one_alert(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """A due staged send the worker cannot move into outbox is left in
        place -- without this it would wait there forever with nothing
        said. Measured from when it was due, not when it was staged; a
        cancelled one is not waiting for anything."""
        account_id = await _seed_inactive_account(migrated_db)
        await _seed_pending_send(
            migrated_db, account_id, overdue_by=timedelta(hours=1), subject="Stuck staged",
        )
        await _seed_pending_send(
            migrated_db, account_id, overdue_by=timedelta(minutes=1), subject="Just due",
        )
        await _seed_pending_send(
            migrated_db, account_id, overdue_by=timedelta(hours=1), subject="Undone",
            cancelled=True,
        )

        await raise_stalled_outbox_alerts_once(migrated_db, None, None, threshold=_THRESHOLD)
        await raise_stalled_outbox_alerts_once(migrated_db, None, None, threshold=_THRESHOLD)

        alerts = await _alerts_for(migrated_db, account_id)
        assert len(alerts) == 1
        assert alerts[0].body is not None and "Stuck staged" in alerts[0].body

    @pytest.mark.asyncio
    async def test_the_alert_is_announced_live(self, migrated_db: DatabaseConnection) -> None:
        """alert.new over SSE is what refreshes an open bell -- the same
        event a new-mail alert sends."""
        account_id = await _seed_inactive_account(migrated_db)
        await _seed_outbox_row(
            migrated_db, account_id, kind="send", status="pending",
            age=timedelta(hours=1), subject="Announced",
        )
        event_ring = EventRing()

        await raise_stalled_outbox_alerts_once(
            migrated_db, event_ring, None, threshold=_THRESHOLD,
        )

        # Across every account: replaying one account's ring from 0 reads as
        # a gap too large to replay and returns nothing at all.
        events = [
            e for e in await event_ring.replay_from(0)
            if e["event_type"] == "alert.new" and e["data"]["account_id"] == str(account_id)
        ]
        assert len(events) == 1
        assert events[0]["data"]["kind"] == "outbox_stalled"
