"""
A stalled-outbox alert resolves itself once the message it was about stops
waiting on the user: its outbox row was sent, or its staged send was
cancelled. A row that went dead keeps its alert -- the message still did
not go out, and that is exactly what the alert is for.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from mail_verdict.api.event_ring import EventRing
from mail_verdict.api.outbox import router as outbox_router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Alert
from mail_verdict.outbox.stalled import (
    DEDUPE_PREFIX,
    raise_stalled_outbox_alerts_once,
    resolve_stalled_for_outbox_event,
)
from tests.pg.test_outbox_stalled_pg import (
    _THRESHOLD,
    _alerts_for,
    _seed_inactive_account,
    _seed_outbox_row,
    _seed_pending_send,
)


async def _set_outbox_status(db: DatabaseConnection, outbox_id: uuid.UUID, status: str) -> None:
    async with db.session() as session:
        await session.execute(
            text("UPDATE outbox SET status = :status WHERE id = :id"),
            {"status": status, "id": outbox_id},
        )


async def _pass(db: DatabaseConnection) -> None:
    await raise_stalled_outbox_alerts_once(db, None, None, threshold=_THRESHOLD)


async def _one_alert_dismissed(db: DatabaseConnection, account_id: uuid.UUID) -> bool:
    alerts = await _alerts_for(db, account_id)
    assert len(alerts) == 1
    return alerts[0].dismissed_at is not None


class TestStalledAlertResolves:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["send", "draft"])
    async def test_a_row_that_is_finally_sent_resolves_its_alert(
        self, migrated_db: DatabaseConnection, kind: str,
    ) -> None:
        account_id = await _seed_inactive_account(migrated_db)
        outbox_id = await _seed_outbox_row(
            migrated_db, account_id, kind=kind, status="pending",
            age=timedelta(hours=1), subject="Eventually sent",
        )
        await _pass(migrated_db)
        assert not await _one_alert_dismissed(migrated_db, account_id)

        await _set_outbox_status(migrated_db, outbox_id, "sent")
        await _pass(migrated_db)

        assert await _one_alert_dismissed(migrated_db, account_id)

    @pytest.mark.asyncio
    async def test_a_cancelled_staged_send_resolves_its_alert(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = await _seed_inactive_account(migrated_db)
        pending_id = await _seed_pending_send(
            migrated_db, account_id, overdue_by=timedelta(hours=1), subject="Then undone",
        )
        await _pass(migrated_db)
        assert not await _one_alert_dismissed(migrated_db, account_id)

        async with migrated_db.session() as session:
            await session.execute(
                text("UPDATE pending_sends SET cancelled_at = now() WHERE id = :id"),
                {"id": pending_id},
            )
        await _pass(migrated_db)

        assert await _one_alert_dismissed(migrated_db, account_id)

    @pytest.mark.asyncio
    async def test_a_staged_send_that_moved_on_and_was_sent_resolves_its_alert(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """A staged send keeps its id when it moves into outbox, so the
        alert raised while it was staged is resolved by the outbox row."""
        account_id = await _seed_inactive_account(migrated_db)
        send_id = await _seed_pending_send(
            migrated_db, account_id, overdue_by=timedelta(hours=1), subject="Moved on",
        )
        await _pass(migrated_db)

        async with migrated_db.session() as session:
            await session.execute(text("DELETE FROM pending_sends WHERE id = :id"), {"id": send_id})
            await session.execute(
                text(
                    "INSERT INTO outbox (id, account_id, kind, to_addrs, subject, body_text, "
                    "status, created_at) VALUES (:id, :account_id, 'send', "
                    "'[\"them@example.com\"]', 'Moved on', 'hi', 'sent', :created_at)"
                ),
                {
                    "id": send_id, "account_id": account_id,
                    "created_at": datetime.now(timezone.utc),
                },
            )
        await _pass(migrated_db)

        assert await _one_alert_dismissed(migrated_db, account_id)

    @pytest.mark.asyncio
    async def test_the_sent_event_resolves_its_own_rows_alert_at_once(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = await _seed_inactive_account(migrated_db)
        sent_id = await _seed_outbox_row(
            migrated_db, account_id, kind="send", status="pending",
            age=timedelta(hours=1), subject="Sent now",
        )
        await _seed_outbox_row(
            migrated_db, account_id, kind="send", status="pending",
            age=timedelta(hours=1), subject="Still waiting",
        )
        await _pass(migrated_db)
        await _set_outbox_status(migrated_db, sent_id, "sent")
        ring = AsyncMock()

        resolved = await resolve_stalled_for_outbox_event(migrated_db, ring, str(sent_id))

        alerts = {a.id: a for a in await _alerts_for(migrated_db, account_id)}
        assert len(resolved) == 1
        assert alerts[resolved[0]].body is not None and "Sent now" in alerts[resolved[0]].body
        assert [a.dismissed_at is None for a in alerts.values()].count(True) == 1
        ring.add.assert_any_await(account_id, "alert.dismissed", {})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["dead", "failed", "processing"])
    async def test_a_row_still_not_sent_keeps_its_alert(
        self, migrated_db: DatabaseConnection, status: str,
    ) -> None:
        account_id = await _seed_inactive_account(migrated_db)
        outbox_id = await _seed_outbox_row(
            migrated_db, account_id, kind="send", status="pending",
            age=timedelta(hours=1), subject="Still stuck",
        )
        await _pass(migrated_db)

        await _set_outbox_status(migrated_db, outbox_id, status)
        await _pass(migrated_db)

        assert not await _one_alert_dismissed(migrated_db, account_id)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(outbox_router)
    with TestClient(app) as c:
        yield c


async def _stalled_alert_dismissed(db: DatabaseConnection, pending_id: uuid.UUID) -> bool:
    async with db.session() as session:
        dismissed_at = (
            await session.execute(
                select(Alert.dismissed_at).where(
                    Alert.dedupe_key == f"{DEDUPE_PREFIX}{pending_id}",
                )
            )
        ).scalar_one()
    return dismissed_at is not None


async def _stall_a_staged_send(db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID]:
    """A staged send stuck past its window, already alerted on."""
    account_id = await _seed_inactive_account(db)
    pending_id = await _seed_pending_send(
        db, account_id, overdue_by=timedelta(hours=1), subject="Undo me",
    )
    await _pass(db)
    assert not await _stalled_alert_dismissed(db, pending_id)
    return account_id, pending_id


class TestUndoClearsTheStalledAlert:
    def test_cancelling_a_stalled_staged_send_resolves_its_alert_in_the_same_request(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """Pressing Undo is what settles the stuck send, so its alert clears
        with the cancel itself rather than on the stalled pass's next tick."""
        account_id, pending_id = client.portal.call(_stall_a_staged_send, migrated_db)
        event_ring = EventRing()
        client.portal.call(event_ring.add, account_id, "test.seed", {})
        seq_before = event_ring.get_latest_seq()

        with (
            patch("mail_verdict.api.outbox.get_db_connection", return_value=migrated_db),
            patch("mail_verdict.api.outbox.get_event_ring", return_value=event_ring),
        ):
            resp = client.post(f"/outbox/pending/{pending_id}/cancel")
        assert resp.status_code == 204, resp.text

        assert client.portal.call(_stalled_alert_dismissed, migrated_db, pending_id)
        new_events = client.portal.call(event_ring.replay_from, seq_before, str(account_id))
        assert any(e["event_type"] == "alert.dismissed" for e in new_events), new_events
