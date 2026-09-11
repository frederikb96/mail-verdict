"""
Read state that follows the mail wherever it goes: a mail alert resolves
itself once its mail is read by any path, and mail in Archive or Trash is
marked read however it got there -- against a real Postgres schema with
PostIMAP's own triggers, so every event below is the payload PostIMAP
actually sends rather than one built by hand.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import AsyncMock

import asyncpg  # type: ignore[import-untyped]
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.alerts.dispatch import (
    _finalize_pending_mail_alerts_once,
    create_mail_alert_for_arrival,
)
from mail_verdict.alerts.resolve import resolve_for_message_event
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Alert, Message
from mail_verdict.database.repository import AlertRepository
from mail_verdict.filing import read_state
from mail_verdict.filing.read_state import mark_read_on_landing, reconcile_read_state_once
from mail_verdict.postimap.actions import move_message, set_flags
from mail_verdict.postimap.listener import CHANNEL, PostimapEvent, parse_dsn_from_sqlalchemy_url
from mail_verdict.settings.service import SettingsService
from tests.pg.test_alerts_pg import _seed_watermark
from tests.pg.test_mark_read_on_file_pg import _seed_account_with_archive

_EVENT_TIMEOUT_S = 10.0


async def _settings(db: DatabaseConnection, *, archive_marks_read: bool = True) -> SettingsService:
    """Written explicitly every time -- the database is shared across the
    whole pg layer, so a default merge would depend on file order."""
    service = SettingsService(db)
    await service.load()
    await service.update("mail", {"mark_read_on_file_to_archive_or_junk": archive_marks_read})
    return service


@contextlib.asynccontextmanager
async def _events(postgres_url: str) -> AsyncIterator[asyncio.Queue[PostimapEvent]]:
    """Every postimap_events payload sent while open, parsed the way the
    application's own listener parses it."""
    conn = await asyncpg.connect(parse_dsn_from_sqlalchemy_url(postgres_url))
    queue: asyncio.Queue[PostimapEvent] = asyncio.Queue()

    def _on_notify(_conn: Any, _pid: int, _channel: str, payload: str) -> None:
        queue.put_nowait(PostimapEvent.from_payload(json.loads(payload)))

    await conn.add_listener(CHANNEL, _on_notify)
    try:
        yield queue
    finally:
        await conn.remove_listener(CHANNEL, _on_notify)
        await conn.close()


async def _next_event(
    queue: asyncio.Queue[PostimapEvent], message_id: uuid.UUID,
    predicate: Callable[[PostimapEvent], bool],
) -> PostimapEvent:
    """The first message event for this row matching `predicate`."""
    deadline = time.monotonic() + _EVENT_TIMEOUT_S
    while True:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"no matching postimap event for message {message_id}"
        event = await asyncio.wait_for(queue.get(), timeout=remaining)
        if event.type == "message" and event.id == str(message_id) and predicate(event):
            return event


async def _as_sync(session: AsyncSession) -> None:
    """Make this transaction's writes PostIMAP's own -- origin "sync", the
    shape a change made in another mail client arrives in."""
    await session.execute(text("SET LOCAL postimap.writer = 'sync'"))


async def _insert_message(
    session: AsyncSession, account_id: uuid.UUID, folder_id: uuid.UUID, *,
    imap_uid: int, header: str | None, is_seen: bool = False,
) -> uuid.UUID:
    message_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
            "from_addr, received_at, is_seen) "
            "VALUES (:id, :account_id, :folder_id, :uid, :thread_id, :header, 'Read state', "
            "'sender@example.com', now(), :is_seen)"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": folder_id,
            "uid": imap_uid, "thread_id": uuid.uuid4(), "header": header, "is_seen": is_seen,
        },
    )
    return message_id


async def _delivered_alert(
    db: DatabaseConnection, account_id: uuid.UUID, message_id: uuid.UUID,
) -> uuid.UUID:
    alert = await AlertRepository(db).create_mail_alert(
        account_id=account_id, message_id=message_id, msg_key=f"test-{uuid.uuid4()}",
        title="Read state", body="sender@example.com", delivered=True,
    )
    assert alert is not None
    return alert.id


async def _alert(db: DatabaseConnection, alert_id: uuid.UUID) -> Alert:
    async with db.session() as session:
        return (await session.execute(select(Alert).where(Alert.id == alert_id))).scalar_one()


async def _is_seen(db: DatabaseConnection, message_id: uuid.UUID) -> bool:
    async with db.session() as session:
        return bool(
            (await session.execute(select(Message.is_seen).where(Message.id == message_id)))
            .scalar_one()
        )


class TestAlertResolvesWhenItsMailIsRead:
    @pytest.mark.asyncio
    async def test_marking_read_through_the_contract_resolves_it(
        self, migrated_db: DatabaseConnection, postgres_url: str,
    ) -> None:
        account_id, inbox_id, *_ = await _seed_account_with_archive(migrated_db)
        async with migrated_db.session() as session:
            message_id = await _insert_message(
                session, account_id, inbox_id, imap_uid=1, header=f"<{uuid.uuid4()}@x>",
            )
        alert_id = await _delivered_alert(migrated_db, account_id, message_id)
        ring = AsyncMock()

        async with _events(postgres_url) as queue:
            async with migrated_db.session() as session:
                await set_flags(session, message_id, is_seen=True)
            event = await _next_event(queue, message_id, lambda e: e.op == "update")

        assert "is_seen" in event.changed
        assert event.origin == "app"
        assert await resolve_for_message_event(migrated_db, ring, event.id) == [alert_id]
        assert (await _alert(migrated_db, alert_id)).dismissed_at is not None
        ring.add.assert_any_await(account_id, "alert.dismissed", {})

    @pytest.mark.asyncio
    async def test_a_read_flag_from_another_client_resolves_it(
        self, migrated_db: DatabaseConnection, postgres_url: str,
    ) -> None:
        account_id, inbox_id, *_ = await _seed_account_with_archive(migrated_db)
        async with migrated_db.session() as session:
            message_id = await _insert_message(
                session, account_id, inbox_id, imap_uid=1, header=f"<{uuid.uuid4()}@x>",
            )
        alert_id = await _delivered_alert(migrated_db, account_id, message_id)

        async with _events(postgres_url) as queue:
            async with migrated_db.session() as session:
                await _as_sync(session)
                await session.execute(
                    text("UPDATE messages SET is_seen = true WHERE id = :id"), {"id": message_id},
                )
            event = await _next_event(queue, message_id, lambda e: e.op == "update")

        assert event.origin == "sync"
        assert "is_seen" in event.changed
        assert await resolve_for_message_event(migrated_db, None, event.id) == [alert_id]

    @pytest.mark.asyncio
    async def test_an_unread_message_leaves_its_alert_alone(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, inbox_id, *_ = await _seed_account_with_archive(migrated_db)
        async with migrated_db.session() as session:
            message_id = await _insert_message(
                session, account_id, inbox_id, imap_uid=1, header=f"<{uuid.uuid4()}@x>",
            )
        alert_id = await _delivered_alert(migrated_db, account_id, message_id)

        assert await resolve_for_message_event(migrated_db, None, str(message_id)) == []
        assert (await _alert(migrated_db, alert_id)).dismissed_at is None

    @pytest.mark.asyncio
    async def test_a_move_to_archive_in_another_client_resolves_it_through_the_new_row(
        self, migrated_db: DatabaseConnection, postgres_url: str,
    ) -> None:
        """The phone case end to end: another client moves an unread
        message to Archive, which PostIMAP mirrors as an expunge plus a
        fresh row. Landing marks the fresh row read; that read resolves an
        alert still naming the expunged original, by header."""
        settings = await _settings(migrated_db)
        account_id, inbox_id, _junk, archive_id, _trash = await _seed_account_with_archive(
            migrated_db,
        )
        header = f"<{uuid.uuid4()}@x>"
        async with migrated_db.session() as session:
            original_id = await _insert_message(
                session, account_id, inbox_id, imap_uid=1, header=header,
            )
        alert_id = await _delivered_alert(migrated_db, account_id, original_id)

        async with _events(postgres_url) as queue:
            async with migrated_db.session() as session:
                await _as_sync(session)
                await session.execute(
                    text("UPDATE messages SET expunged_at = now() WHERE id = :id"),
                    {"id": original_id},
                )
                moved_id = await _insert_message(
                    session, account_id, archive_id, imap_uid=1, header=header,
                )
            landed = await _next_event(queue, moved_id, lambda e: e.op == "insert")
            assert landed.origin == "sync"
            assert landed.folder_id == str(archive_id)

            assert await mark_read_on_landing(migrated_db, settings, landed) == 1
            read = await _next_event(queue, moved_id, lambda e: "is_seen" in e.changed)

        assert await resolve_for_message_event(migrated_db, None, read.id) == [alert_id]

    @pytest.mark.asyncio
    async def test_a_row_arriving_already_read_resolves_it_on_insert(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Read first, moved afterwards: the new row is read from the start,
        so no read-state change ever follows its insert."""
        account_id, inbox_id, _junk, archive_id, _trash = await _seed_account_with_archive(
            migrated_db,
        )
        header = f"<{uuid.uuid4()}@x>"
        async with migrated_db.session() as session:
            original_id = await _insert_message(
                session, account_id, inbox_id, imap_uid=1, header=header,
            )
        alert_id = await _delivered_alert(migrated_db, account_id, original_id)
        async with migrated_db.session() as session:
            await session.execute(
                text("UPDATE messages SET expunged_at = now() WHERE id = :id"),
                {"id": original_id},
            )
            moved_id = await _insert_message(
                session, account_id, archive_id, imap_uid=1, header=header, is_seen=True,
            )

        assert await resolve_for_message_event(migrated_db, None, str(moved_id)) == [alert_id]

    @pytest.mark.asyncio
    async def test_a_staged_alert_whose_mail_was_read_is_never_delivered(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        settings = await _settings(migrated_db)
        account_id, inbox_id, *_ = await _seed_account_with_archive(migrated_db)
        async with migrated_db.session() as session:
            await _seed_watermark(session, account_id=account_id, folder_id=inbox_id)
            message_id = await _insert_message(
                session, account_id, inbox_id, imap_uid=1, header=f"<{uuid.uuid4()}@x>",
            )
        ring = AsyncMock()
        await create_mail_alert_for_arrival(
            migrated_db, ring, None, account_id=account_id, message_id=message_id,
            settings_service=settings, folder_id=inbox_id,
        )
        async with migrated_db.session() as session:
            alert = (
                await session.execute(select(Alert).where(Alert.message_id == message_id))
            ).scalar_one()
            assert alert.delivered_at is None
            await set_flags(session, message_id, is_seen=True)

        await _finalize_pending_mail_alerts_once(migrated_db, ring, None, settings)

        resolved = await _alert(migrated_db, alert.id)
        assert resolved.dismissed_at is not None
        announced = [call.args[1] for call in ring.add.await_args_list]
        assert "alert.new" not in announced

    @pytest.mark.asyncio
    async def test_mail_read_before_its_alert_existed_is_not_announced(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        settings = await _settings(migrated_db)
        account_id, _inbox, _junk, archive_id, _trash = await _seed_account_with_archive(
            migrated_db,
        )
        async with migrated_db.session() as session:
            message_id = await _insert_message(
                session, account_id, archive_id, imap_uid=1, header=f"<{uuid.uuid4()}@x>",
                is_seen=True,
            )
        ring = AsyncMock()
        await create_mail_alert_for_arrival(
            migrated_db, ring, None, account_id=account_id, message_id=message_id,
            settings_service=settings, folder_id=archive_id,
        )

        async with migrated_db.session() as session:
            alert = (
                await session.execute(select(Alert).where(Alert.message_id == message_id))
            ).scalar_one()
        assert alert.dismissed_at is not None
        ring.add.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_sweep_resolves_what_no_event_reported(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        settings = await _settings(migrated_db)
        account_id, inbox_id, *_ = await _seed_account_with_archive(migrated_db)
        async with migrated_db.session() as session:
            read_id = await _insert_message(
                session, account_id, inbox_id, imap_uid=1, header=f"<{uuid.uuid4()}@x>",
                is_seen=True,
            )
            unread_id = await _insert_message(
                session, account_id, inbox_id, imap_uid=2, header=f"<{uuid.uuid4()}@x>",
            )
        read_alert = await _delivered_alert(migrated_db, account_id, read_id)
        unread_alert = await _delivered_alert(migrated_db, account_id, unread_id)

        await reconcile_read_state_once(migrated_db, None, settings)

        assert (await _alert(migrated_db, read_alert)).dismissed_at is not None
        assert (await _alert(migrated_db, unread_alert)).dismissed_at is None


class TestArchiveAndTrashAreRead:
    @pytest.mark.asyncio
    async def test_landing_in_archive_or_trash_marks_read_and_the_inbox_does_not(
        self, migrated_db: DatabaseConnection, postgres_url: str,
    ) -> None:
        settings = await _settings(migrated_db)
        account_id, inbox_id, _junk, archive_id, trash_id = await _seed_account_with_archive(
            migrated_db,
        )
        async with _events(postgres_url) as queue:
            async with migrated_db.session() as session:
                await _as_sync(session)
                ids = {
                    folder_id: await _insert_message(
                        session, account_id, folder_id, imap_uid=1, header=f"<{uuid.uuid4()}@x>",
                    )
                    for folder_id in (inbox_id, archive_id, trash_id)
                }
            for message_id in ids.values():
                event = await _next_event(queue, message_id, lambda e: e.op == "insert")
                await mark_read_on_landing(migrated_db, settings, event)

        assert not await _is_seen(migrated_db, ids[inbox_id])
        assert await _is_seen(migrated_db, ids[archive_id])
        assert await _is_seen(migrated_db, ids[trash_id])

    @pytest.mark.asyncio
    async def test_a_move_made_through_the_contract_into_trash_marks_read(
        self, migrated_db: DatabaseConnection, postgres_url: str,
    ) -> None:
        settings = await _settings(migrated_db)
        account_id, inbox_id, _junk, _archive, trash_id = await _seed_account_with_archive(
            migrated_db,
        )
        async with migrated_db.session() as session:
            message_id = await _insert_message(
                session, account_id, inbox_id, imap_uid=1, header=f"<{uuid.uuid4()}@x>",
            )
        async with _events(postgres_url) as queue:
            async with migrated_db.session() as session:
                await move_message(session, message_id, trash_id)
            event = await _next_event(queue, message_id, lambda e: "folder_id" in e.changed)

        assert await mark_read_on_landing(migrated_db, settings, event) == 1
        assert await _is_seen(migrated_db, message_id)

    @pytest.mark.asyncio
    async def test_with_archiving_left_unread_only_trash_is_marked(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        settings = await _settings(migrated_db, archive_marks_read=False)
        account_id, _inbox, _junk, archive_id, trash_id = await _seed_account_with_archive(
            migrated_db,
        )
        async with migrated_db.session() as session:
            archived = await _insert_message(
                session, account_id, archive_id, imap_uid=1, header=f"<{uuid.uuid4()}@x>",
            )
            trashed = await _insert_message(
                session, account_id, trash_id, imap_uid=1, header=f"<{uuid.uuid4()}@x>",
            )

        await reconcile_read_state_once(migrated_db, None, settings)

        assert not await _is_seen(migrated_db, archived)
        assert await _is_seen(migrated_db, trashed)

    @pytest.mark.asyncio
    async def test_the_reconcile_marks_stragglers_and_leaves_the_inbox(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        settings = await _settings(migrated_db)
        account_id, inbox_id, _junk, archive_id, trash_id = await _seed_account_with_archive(
            migrated_db,
        )
        async with migrated_db.session() as session:
            inbox_unread = await _insert_message(
                session, account_id, inbox_id, imap_uid=1, header=f"<{uuid.uuid4()}@x>",
            )
            stragglers = [
                await _insert_message(
                    session, account_id, folder_id, imap_uid=uid, header=f"<{uuid.uuid4()}@x>",
                )
                for folder_id in (archive_id, trash_id)
                for uid in (1, 2)
            ]
            # A move made here and not yet landed: no UID yet.
            pending = await _insert_message(
                session, account_id, inbox_id, imap_uid=3, header=f"<{uuid.uuid4()}@x>",
            )
            await move_message(session, pending, archive_id)
            expunged = await _insert_message(
                session, account_id, archive_id, imap_uid=9, header=f"<{uuid.uuid4()}@x>",
            )
            await session.execute(
                text("UPDATE messages SET expunged_at = now() WHERE id = :id"), {"id": expunged},
            )

        await reconcile_read_state_once(migrated_db, None, settings)

        for message_id in [*stragglers, pending]:
            assert await _is_seen(migrated_db, message_id)
        assert not await _is_seen(migrated_db, inbox_unread)
        assert not await _is_seen(migrated_db, expunged)

    @pytest.mark.asyncio
    async def test_an_unread_message_deep_in_a_large_folder_is_reached_over_ticks(
        self, migrated_db: DatabaseConnection, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each tick reads at most two windows per folder, so a straggler
        below them is reached by the cursor over later ticks rather than by
        a tick reading the whole folder."""
        monkeypatch.setattr(read_state, "_WINDOW", 10)
        settings = await _settings(migrated_db)
        account_id, _inbox, _junk, archive_id, _trash = await _seed_account_with_archive(
            migrated_db,
        )
        async with migrated_db.session() as session:
            await session.execute(
                text(
                    "INSERT INTO messages (account_id, folder_id, imap_uid, thread_id, "
                    "message_id, received_at, is_seen) "
                    "SELECT :account_id, :folder_id, g, gen_random_uuid(), "
                    "'<deep-' || g || '-' || :tag || '@x>', now(), g <> 1 "
                    "FROM generate_series(1, 55) g"
                ),
                {"account_id": account_id, "folder_id": archive_id, "tag": uuid.uuid4().hex},
            )
            deep_id = (
                await session.execute(
                    text("SELECT id FROM messages WHERE folder_id = :f AND imap_uid = 1"),
                    {"f": archive_id},
                )
            ).scalar_one()

        cursors: read_state.Cursors = {}
        ticks = 0
        while not await _is_seen(migrated_db, deep_id):
            ticks += 1
            assert ticks <= 5, "the cursor never reached the bottom of the folder"
            await reconcile_read_state_once(migrated_db, None, settings, cursors)

        # 55 rows, 10 per window: the top window plus one deeper window per
        # tick reaches uid 1 on the fifth tick, not the first.
        assert ticks == 5


# Larger than any real archive this runs against, and large enough that a
# plan reading the whole folder (hundreds of pages) would exceed the
# buffer bound below several times over. Seeding goes through PostIMAP's
# per-row triggers, which is what keeps it from being larger here.
_LARGE_ARCHIVE_ROWS = 20_000


def _plan_nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [plan]
    for child in plan.get("Plans", []):
        nodes.extend(_plan_nodes(child))
    return nodes


async def _explain(session: AsyncSession, sql: str, params: dict[str, Any]) -> dict[str, Any]:
    result = await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}"), params)
    raw = result.scalar_one()
    return (json.loads(raw) if isinstance(raw, str) else raw)[0]["Plan"]


class TestReconcileCostOnALargeArchive:
    @pytest.mark.asyncio
    async def test_each_step_reads_a_bounded_slice_through_existing_indexes(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The reconcile's statements, planned against an archive far
        larger than any real one: the gate is a primary-key read, and a
        window -- from the top or below a cursor -- is a backward walk of
        PostIMAP's own (folder_id, imap_uid) index that stops after one
        window, never a scan of the folder."""
        account_id, _inbox, _junk, archive_id, _trash = await _seed_account_with_archive(
            migrated_db,
        )
        try:
            async with migrated_db.session() as session:
                # Mirrored the way a first sync would be, events suppressed.
                await session.execute(text("SET LOCAL postimap.backfill = 'on'"))
                await session.execute(
                    text(
                        "INSERT INTO messages (account_id, folder_id, imap_uid, thread_id, "
                        "message_id, received_at, is_seen) "
                        "SELECT :account_id, :folder_id, g, gen_random_uuid(), "
                        "'<large-' || g || '-' || :tag || '@x>', now(), g % 50000 <> 7 "
                        "FROM generate_series(1, :rows) g"
                    ),
                    {
                        "account_id": account_id, "folder_id": archive_id,
                        "tag": uuid.uuid4().hex, "rows": _LARGE_ARCHIVE_ROWS,
                    },
                )
            async with migrated_db.engine.connect() as conn:
                await conn.execution_options(isolation_level="AUTOCOMMIT")
                await conn.execute(text("ANALYZE messages"))
                await conn.execute(text("ANALYZE folders"))

            async with migrated_db.session() as session:
                gate = await _explain(
                    session, "SELECT unread_count FROM folders WHERE id = :id", {"id": archive_id},
                )
                gate_nodes = _plan_nodes(gate)
                assert "Seq Scan" not in {n["Node Type"] for n in gate_nodes}, gate_nodes
                assert "folders_pkey" in {n.get("Index Name") for n in gate_nodes}, gate_nodes

                for below_uid in (None, _LARGE_ARCHIVE_ROWS // 2):
                    plan = await _explain(
                        session, read_state.window_sql(below=below_uid is not None),
                        {"folder_id": archive_id, "below_uid": below_uid,
                         "window": read_state._WINDOW},
                    )
                    nodes = _plan_nodes(plan)
                    scans = [n for n in nodes if "Scan" in n["Node Type"]]
                    assert [n["Node Type"] for n in scans] == ["Index Scan"], nodes
                    assert scans[0]["Index Name"] == "idx_msg_folder_uid_live"
                    assert scans[0]["Scan Direction"] == "Backward"
                    assert scans[0]["Actual Rows"] <= read_state._WINDOW
                    blocks = plan["Shared Hit Blocks"] + plan["Shared Read Blocks"]
                    # One window's heap pages plus the index path, not the
                    # folder's several thousand pages.
                    assert blocks < 100, blocks

            # A whole tick against it walks exactly two windows down from the
            # top and leaves the cursor there, however much folder remains.
            settings = await _settings(migrated_db)
            cursors: read_state.Cursors = {}
            await reconcile_read_state_once(migrated_db, None, settings, cursors)
            assert cursors[archive_id] == _LARGE_ARCHIVE_ROWS - 2 * read_state._WINDOW + 1
        finally:
            async with migrated_db.session() as session:
                await session.execute(text("SET LOCAL postimap.backfill = 'on'"))
                await session.execute(
                    text("DELETE FROM accounts WHERE id = :id"), {"id": account_id},
                )
