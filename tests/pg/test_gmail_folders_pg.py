"""
Mail that a server files under several folders -- Gmail's labels and All
Mail -- is classified and embedded once: a folder can be switched out of
the pipeline and the semantic index (folder_prefs.pipeline_enabled), the
folder holding everything is the Archive, and a new Gmail account's label
folders start switched off.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.folder_management import update_folder_prefs
from mail_verdict.api.schemas import FolderPrefsUpdate
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import EMBEDDING_DIMENSIONS, MessageEmbedding
from mail_verdict.embeddings.repository import EmbeddingRepository
from mail_verdict.pipeline.enqueue import enqueue_live_arrival
from mail_verdict.postimap.listener import PostimapEvent
from mail_verdict.providers import apply_folder_defaults, apply_folder_defaults_for_event
from mail_verdict.queue.work_queue import WorkQueue
from tests.pg.test_embedding_gate import (
    _delete_pipeline_runs,
    _seed_message,
    _seed_synced_account_and_inbox,
    _settings,
    _unique_model,
)


async def _add_folder(
    session: AsyncSession, account_id: uuid.UUID, name: str, *,
    special_use: str | None = None, watermarked: bool = True,
) -> uuid.UUID:
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use, initial_sync_done) "
            "VALUES (:id, :account_id, :name, :special_use, true)"
        ),
        {"id": folder_id, "account_id": account_id, "name": name, "special_use": special_use},
    )
    if watermarked:
        await session.execute(
            text(
                "INSERT INTO pipeline_folder_state (folder_id, account_id, backfill_completed_at) "
                "VALUES (:folder_id, :account_id, now() - interval '1 hour')"
            ),
            {"folder_id": folder_id, "account_id": account_id},
        )
    return folder_id


async def _copy_of(
    session: AsyncSession, original: uuid.UUID, folder_id: uuid.UUID,
) -> uuid.UUID:
    """The same message (same Message-ID header) filed in another folder."""
    copy_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, from_addr, "
            "subject, body_text, received_at, size_bytes, created_at) "
            "SELECT :copy_id, account_id, :folder_id, imap_uid + 1000, thread_id, message_id, "
            "from_addr, subject, body_text, received_at, size_bytes, created_at "
            "FROM messages WHERE id = :original"
        ),
        {"copy_id": copy_id, "folder_id": folder_id, "original": original},
    )
    return copy_id


async def _set_prefs(
    session: AsyncSession, folder_id: uuid.UUID, *,
    pipeline_enabled: bool | None = None, special_use_override: str | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO folder_prefs (folder_id, pipeline_enabled, special_use_override) "
            "VALUES (:folder_id, :enabled, :override)"
        ),
        {"folder_id": folder_id, "enabled": pipeline_enabled, "override": special_use_override},
    )


async def _embedding_message_ids(db: DatabaseConnection, account_id: uuid.UUID) -> set[uuid.UUID]:
    async with db.session() as session:
        rows = await session.execute(
            text("SELECT message_id FROM message_embeddings WHERE account_id = :a"),
            {"a": account_id},
        )
        return {row.message_id for row in rows}


@pytest.mark.asyncio
async def test_a_switched_off_folder_is_never_embedded(migrated_db: DatabaseConnection) -> None:
    settings_service = await _settings(migrated_db)
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id, inbox_id = await _seed_synced_account_and_inbox(session)
        label_id = await _add_folder(session, account_id, "Receipts")
        await _set_prefs(session, label_id, pipeline_enabled=False)
        inbox_mail = await _seed_message(session, account_id=account_id, folder_id=inbox_id)
        label_mail = await _copy_of(session, inbox_mail, label_id)
        await session.commit()

    event = PostimapEvent(
        v=1, type="message", op="insert", id=str(label_mail), account_id=str(account_id),
        origin="sync",
    )
    await enqueue_live_arrival(migrated_db, event, settings_service)
    assert await _embedding_message_ids(migrated_db, account_id) == set()

    candidates, inserted = await EmbeddingRepository(migrated_db).enqueue_missing_batch(
        model=model, batch_size=50, account_id=account_id,
    )

    assert (candidates, inserted) == (1, 1)
    assert await _embedding_message_ids(migrated_db, account_id) == {inbox_mail}
    status = await EmbeddingRepository(migrated_db).status(model=model, account_id=account_id)
    assert status.in_scope == 1


@pytest.mark.asyncio
async def test_mail_in_the_inbox_is_classified_when_its_embedding_sits_on_the_archive_copy(
    migrated_db: DatabaseConnection,
) -> None:
    """The embedding belongs to whichever copy was embedded first. When
    that is the All Mail copy, the run goes to the inbox copy -- All Mail
    itself never runs the pipeline, so nothing else would classify it."""
    settings_service = await _settings(migrated_db)
    embedding_repo = EmbeddingRepository(migrated_db)
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id, inbox_id = await _seed_synced_account_and_inbox(session)
        all_mail_id = await _add_folder(session, account_id, "[Gmail]/All Mail", special_use="all")
        await _set_prefs(session, all_mail_id, special_use_override="archive")
        inbox_mail = await _seed_message(session, account_id=account_id, folder_id=inbox_id)
        archive_mail = await _copy_of(session, inbox_mail, all_mail_id)
        await session.commit()

    assert await embedding_repo.enqueue_one(
        account_id=account_id, message_id=archive_mail, model=model,
    )
    assert not await embedding_repo.enqueue_one(
        account_id=account_id, message_id=inbox_mail, model=model,
    )
    work_queue = WorkQueue(migrated_db, MessageEmbedding.__table__)
    claimed = await work_queue.claim_batch(worker_id="w1", batch_size=10, lease_seconds=30)
    item = next(row for row in claimed if row["message_id"] == archive_mail)
    assert await embedding_repo.write_result(
        item["id"], worker_id="w1", embedding=[0.1] * EMBEDDING_DIMENSIONS, model=model,
        content_level="full", source_hash="abc", settings_service=settings_service,
    )

    async with migrated_db.session() as session:
        runs = (
            await session.execute(
                text("SELECT message_id FROM pipeline_runs WHERE account_id = :a"),
                {"a": account_id},
            )
        ).all()
    assert [row.message_id for row in runs] == [inbox_mail]

    await _delete_pipeline_runs(migrated_db, account_id)


@pytest.mark.asyncio
async def test_a_switched_off_folder_never_gets_a_pipeline_run(
    migrated_db: DatabaseConnection,
) -> None:
    settings_service = await _settings(migrated_db)
    embedding_repo = EmbeddingRepository(migrated_db)
    model = _unique_model()
    async with migrated_db.session() as session:
        account_id, _inbox_id = await _seed_synced_account_and_inbox(session)
        label_id = await _add_folder(session, account_id, "Receipts")
        await _set_prefs(session, label_id, pipeline_enabled=False)
        label_mail = await _seed_message(session, account_id=account_id, folder_id=label_id)
        await session.commit()

    # An embedding that exists anyway (written before the switch was turned off).
    async with migrated_db.session() as session:
        await session.execute(
            text(
                "INSERT INTO message_embeddings (account_id, msg_key, message_id, model) "
                "SELECT account_id, :key, id, :model FROM messages WHERE id = :id"
            ),
            {"key": f"k-{uuid.uuid4()}", "model": model, "id": label_mail},
        )
        await session.commit()
    work_queue = WorkQueue(migrated_db, MessageEmbedding.__table__)
    claimed = await work_queue.claim_batch(worker_id="w1", batch_size=50, lease_seconds=30)
    item = next(row for row in claimed if row["message_id"] == label_mail)
    await embedding_repo.write_result(
        item["id"], worker_id="w1", embedding=[0.1] * EMBEDDING_DIMENSIONS, model=model,
        content_level="full", source_hash="abc", settings_service=settings_service,
    )

    async with migrated_db.session() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM pipeline_runs WHERE account_id = :a"),
                {"a": account_id},
            )
        ).scalar_one()
    assert count == 0


async def _prefs_of(db: DatabaseConnection, folder_id: uuid.UUID) -> tuple[str | None, bool | None]:
    async with db.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT special_use_override, pipeline_enabled FROM folder_prefs "
                    "WHERE folder_id = :id"
                ),
                {"id": folder_id},
            )
        ).one_or_none()
    return (row.special_use_override, row.pipeline_enabled) if row else (None, None)


async def _seed_account(session: AsyncSession, host: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, :host, 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}", "host": host},
    )
    return account_id


@pytest.mark.asyncio
async def test_a_gmail_account_defaults_its_folders(migrated_db: DatabaseConnection) -> None:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session, "imap.gmail.com")
        folders = {
            name: await _add_folder(session, account_id, name, special_use=role)
            for name, role in {
                "INBOX": "inbox", "[Gmail]/Sent Mail": "sent", "[Gmail]/All Mail": "all",
                "[Gmail]/Starred": "flagged", "[Gmail]/Important": None, "Receipts": None,
            }.items()
        }
        for folder_id in folders.values():
            await apply_folder_defaults(session, account_id=account_id, folder_id=folder_id)
        await session.commit()

    prefs = {name: await _prefs_of(migrated_db, fid) for name, fid in folders.items()}

    assert prefs == {
        "INBOX": (None, None), "[Gmail]/Sent Mail": (None, None),
        "[Gmail]/All Mail": ("archive", None),
        "[Gmail]/Starred": (None, False), "[Gmail]/Important": (None, False),
        "Receipts": (None, False),
    }


@pytest.mark.asyncio
async def test_another_server_keeps_its_folders_except_the_one_holding_everything(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session, "imap.example.com")
        everything = await _add_folder(session, account_id, "Everything", special_use="all")
        plain = await _add_folder(session, account_id, "Projects")
        for folder_id in (everything, plain):
            await apply_folder_defaults(session, account_id=account_id, folder_id=folder_id)
        await session.commit()

    assert await _prefs_of(migrated_db, everything) == ("archive", None)
    assert await _prefs_of(migrated_db, plain) == (None, None)


@pytest.mark.asyncio
async def test_a_choice_made_since_survives_the_defaults(migrated_db: DatabaseConnection) -> None:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session, "imap.gmail.com")
        label = await _add_folder(session, account_id, "Receipts")
        everything = await _add_folder(session, account_id, "All Mail", special_use="all")
        await _set_prefs(session, label, pipeline_enabled=True)
        await _set_prefs(session, everything, special_use_override="trash")
        for folder_id in (label, everything):
            await apply_folder_defaults(session, account_id=account_id, folder_id=folder_id)
        await session.commit()

    assert await _prefs_of(migrated_db, label) == (None, True)
    assert await _prefs_of(migrated_db, everything) == ("trash", None)


@pytest.mark.asyncio
async def test_the_folder_prefs_route_switches_a_folder_and_reports_the_effective_state(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        account_id, _inbox = await _seed_synced_account_and_inbox(session)
        label = await _add_folder(session, account_id, "Receipts")
        trash = await _add_folder(session, account_id, "Trash", special_use="trash")
        await session.commit()

    switched_off = await update_folder_prefs(label, FolderPrefsUpdate(pipeline_enabled=False))
    switched_on = await update_folder_prefs(label, FolderPrefsUpdate(pipeline_enabled=True))
    reset = await update_folder_prefs(label, FolderPrefsUpdate(pipeline_enabled=None))
    forced_trash = await update_folder_prefs(trash, FolderPrefsUpdate(pipeline_enabled=True))

    assert switched_off.pipeline_enabled is False
    assert switched_on.pipeline_enabled is True
    assert reset.pipeline_enabled is True
    assert await _prefs_of(migrated_db, label) == (None, None)
    assert forced_trash.pipeline_enabled is False


@pytest.mark.asyncio
async def test_a_folder_event_sets_the_defaults_of_the_folder_it_names(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session, "imap.googlemail.com")
        label = await _add_folder(session, account_id, "Receipts", watermarked=False)
        await session.commit()

    await apply_folder_defaults_for_event(
        migrated_db,
        PostimapEvent(
            v=1, type="folder", op="insert", id=str(label), account_id=str(account_id),
            folder_id=str(label), origin="sync",
        ),
    )

    assert await _prefs_of(migrated_db, label) == (None, False)
