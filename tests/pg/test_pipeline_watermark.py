"""
record_folder_watermark: the watermark a folder's first sync completing
writes into pipeline_folder_state, and specifically that it is taken from
PostIMAP's own clock rather than from now() read at handler time.

This handler runs asynchronously off a NOTIFY -- listener dispatch queue
latency, and PostIMAP's four concurrent dispatch workers processing
events out of order -- so a wall-clock read here is always later than
the moment PostIMAP actually finished the backfill, by whatever that gap
happens to be. Any message inserted in that gap gets a created_at earlier
than such a watermark and is permanently excluded, since both
enqueue_pipeline_run_if_live_eligible and _reconcile_once compare against
the same stored value and it is never moved once set (see the ON
CONFLICT ... COALESCE in the handler itself).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.pipeline.enqueue import record_folder_watermark
from mail_verdict.postimap.listener import PostimapEvent


async def _seed_account_and_folder(
    session: AsyncSession, *, initial_sync_done: bool,
) -> tuple[uuid.UUID, uuid.UUID]:
    account_id, folder_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"watermark-race-{account_id.hex[:8]}"},
    )
    # last_synced_at mirrors what PostIMAP's updateFolderState writes just
    # before the initial_sync_done flip in the same fullSync call --
    # ordinary folder creation stands in for that here.
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, initial_sync_done, last_synced_at) "
            "VALUES (:id, :account_id, 'INBOX', :done, now())"
        ),
        {"id": folder_id, "account_id": account_id, "done": initial_sync_done},
    )
    return account_id, folder_id


async def _seed_message(
    session: AsyncSession, *, account_id: uuid.UUID, folder_id: uuid.UUID, imap_uid: int,
) -> uuid.UUID:
    mail_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, from_addr, "
            "subject, body_text, received_at, size_bytes, created_at) "
            "VALUES (:id, :account_id, :folder_id, :imap_uid, :thread_id, :message_id, "
            "'sender@example.com', 'live mail', 'Body.', now(), 1024, now())"
        ),
        {
            "id": mail_id, "account_id": account_id, "folder_id": folder_id,
            "imap_uid": imap_uid, "thread_id": uuid.uuid4(),
            "message_id": f"<{uuid.uuid4()}@example.com>",
        },
    )
    return mail_id


@pytest.mark.asyncio
async def test_a_message_arriving_before_the_handler_runs_is_still_live_eligible(
    migrated_db: DatabaseConnection,
) -> None:
    """The exact race the finding describes: PostIMAP's own flip already
    happened (last_synced_at is set), a live message lands, and only
    *then* does this application get around to handling the sync_complete
    event -- simulating NOTIFY latency plus dispatch queue backlog."""
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(
            session, initial_sync_done=False,
        )
        await session.commit()

    # PostIMAP's own flip: initial_sync_done -> true. last_synced_at was
    # already set moments earlier by updateFolderState, in the same
    # fullSync call, and this UPDATE does not touch it.
    async with migrated_db.session() as session:
        await session.execute(
            text("UPDATE folders SET initial_sync_done = true WHERE id = :f"),
            {"f": folder_id},
        )
        await session.commit()

    await asyncio.sleep(0.1)

    # A message arrives after the flip but before this application's
    # handler gets to it -- it must still become live-eligible.
    async with migrated_db.session() as session:
        live_id = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, imap_uid=1,
        )
        await session.commit()

    await asyncio.sleep(0.1)

    # The handler finally runs, well after the message above already
    # committed -- standing in for a backed-up dispatch queue.
    event = PostimapEvent(
        v=1, type="folder", op="sync_complete", id=str(folder_id),
        account_id=str(account_id), folder_id=str(folder_id), origin="sync", backfill=True,
    )
    await record_folder_watermark(migrated_db, event)

    async with migrated_db.session() as session:
        watermark = (
            await session.execute(
                text(
                    "SELECT backfill_completed_at FROM pipeline_folder_state "
                    "WHERE folder_id = :f"
                ),
                {"f": folder_id},
            )
        ).scalar_one()
        live_created_at = (
            await session.execute(
                text("SELECT created_at FROM messages WHERE id = :m"), {"m": live_id},
            )
        ).scalar_one()

    assert live_created_at > watermark, (
        "a message that arrived after PostIMAP's own flip must be live-eligible, "
        "however long this handler took to run"
    )


@pytest.mark.asyncio
async def test_a_message_that_predates_the_flip_is_still_excluded(
    migrated_db: DatabaseConnection,
) -> None:
    """The watermark must not become so permissive that it reclassifies
    genuinely historical mail -- only PostIMAP's own clock decides the
    boundary, not "whatever this handler happens to read"."""
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(
            session, initial_sync_done=False,
        )
        # A backfilled message, inserted (and thus created_at-stamped)
        # strictly before PostIMAP's own flip below.
        historical_id = await _seed_message(
            session, account_id=account_id, folder_id=folder_id, imap_uid=1,
        )
        await session.commit()

    await asyncio.sleep(0.1)

    async with migrated_db.session() as session:
        await session.execute(
            text(
                "UPDATE folders SET initial_sync_done = true, last_synced_at = now() "
                "WHERE id = :f"
            ),
            {"f": folder_id},
        )
        await session.commit()

    event = PostimapEvent(
        v=1, type="folder", op="sync_complete", id=str(folder_id),
        account_id=str(account_id), folder_id=str(folder_id), origin="sync", backfill=True,
    )
    await record_folder_watermark(migrated_db, event)

    async with migrated_db.session() as session:
        watermark = (
            await session.execute(
                text(
                    "SELECT backfill_completed_at FROM pipeline_folder_state "
                    "WHERE folder_id = :f"
                ),
                {"f": folder_id},
            )
        ).scalar_one()
        historical_created_at = (
            await session.execute(
                text("SELECT created_at FROM messages WHERE id = :m"), {"m": historical_id},
            )
        ).scalar_one()

    assert historical_created_at <= watermark


@pytest.mark.asyncio
async def test_a_resync_never_moves_the_watermark_backward(
    migrated_db: DatabaseConnection,
) -> None:
    """A later resync of an already-synced folder does not repeat this
    event in production, but if it ever did, the stored watermark must
    win over whatever last_synced_at reads by then -- exactly what the
    ON CONFLICT ... COALESCE already guarantees, proven directly."""
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(
            session, initial_sync_done=True,
        )
        await session.execute(
            text(
                "INSERT INTO pipeline_folder_state (folder_id, account_id, "
                "backfill_completed_at) VALUES (:f, :a, now() - interval '1 day')"
            ),
            {"f": folder_id, "a": account_id},
        )
        await session.commit()

    async with migrated_db.session() as session:
        original = (
            await session.execute(
                text(
                    "SELECT backfill_completed_at FROM pipeline_folder_state "
                    "WHERE folder_id = :f"
                ),
                {"f": folder_id},
            )
        ).scalar_one()

    event = PostimapEvent(
        v=1, type="folder", op="sync_complete", id=str(folder_id),
        account_id=str(account_id), folder_id=str(folder_id), origin="sync", backfill=True,
    )
    await record_folder_watermark(migrated_db, event)

    async with migrated_db.session() as session:
        after = (
            await session.execute(
                text(
                    "SELECT backfill_completed_at FROM pipeline_folder_state "
                    "WHERE folder_id = :f"
                ),
                {"f": folder_id},
            )
        ).scalar_one()

    assert after == original
