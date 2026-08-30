"""
folder_prefs.special_use_override, on a server that never advertises
SPECIAL-USE: the folder repository and the enqueue query already coalesce
it in (pipeline/enqueue.py, database/repository.py's
get_effective_special_use); this file proves the pipeline's own resolver
and message loader do too.

Reuses the account/folder/message/runner helpers from
test_pipeline_runner.py rather than duplicating them, seeding a folder
with an override instead of a raw special_use column.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.repository import AccountPrefsRepository
from mail_verdict.pipeline.context import FolderResolver
from mail_verdict.pipeline.message_view import load_message_view
from tests.pg.test_pipeline_runner import _enqueue_and_run, _make_runner, _seed_message


async def _seed_account_with_override_junk(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """
    An account with an INBOX and a folder named "Spam" whose raw
    special_use is NULL -- the server never advertised SPECIAL-USE -- but
    which folder_prefs.special_use_override names 'junk'. Returns
    (account_id, inbox_folder_id, override_junk_folder_id).
    """
    account_id = uuid.uuid4()
    inbox_id = uuid.uuid4()
    spam_id = uuid.uuid4()

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
    # special_use deliberately NULL -- this server never advertised it.
    await session.execute(
        text("INSERT INTO folders (id, account_id, imap_name) VALUES (:id, :account_id, 'Spam')"),
        {"id": spam_id, "account_id": account_id},
    )
    await session.execute(
        text(
            "INSERT INTO folder_prefs (folder_id, special_use_override) "
            "VALUES (:folder_id, 'junk')"
        ),
        {"folder_id": spam_id},
    )
    return account_id, inbox_id, spam_id


@pytest.mark.asyncio
async def test_folder_resolver_resolves_special_use_through_the_override(
    migrated_db: DatabaseConnection,
) -> None:
    """FolderResolver.resolve(special_use='junk') must find a folder whose
    only claim to being junk is folder_prefs, not the raw column."""
    async with migrated_db.session() as session:
        account_id, _inbox_id, spam_id = await _seed_account_with_override_junk(session)
        await session.commit()

    resolver = FolderResolver(migrated_db, account_id)
    resolved = await resolver.resolve(special_use="junk")

    assert resolved == spam_id


@pytest.mark.asyncio
async def test_message_view_folder_reflects_the_override(migrated_db: DatabaseConnection) -> None:
    """A message sitting in the override-only folder must present as
    special_use='junk' in the runner's own re-check, or a later stage's
    scope re-check disagrees with the enqueue-time gate it claims to
    mirror."""
    async with migrated_db.session() as session:
        account_id, _inbox_id, spam_id = await _seed_account_with_override_junk(session)
        mail_id, _header = await _seed_message(session, account_id=account_id, folder_id=spam_id)
        await session.commit()

    async with migrated_db.session() as session:
        view = await load_message_view(session, mail_id)

    assert view is not None
    assert view.folder.special_use == "junk"


@pytest.mark.asyncio
async def test_move_spam_stage_resolves_junk_through_the_override(
    migrated_db: DatabaseConnection,
) -> None:
    """
    End to end: the default classify + move-spam definition must not
    fail permanently on an account whose junk folder is only known
    through folder_prefs.special_use_override -- before the fix, the
    move-spam stage's Move(special_use='junk') effect could never
    resolve a folder here, so every spam message's run failed with
    StageMisconfigured and nothing was ever filed.
    """
    async with migrated_db.session() as session:
        account_id, inbox_id, spam_id = await _seed_account_with_override_junk(session)
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
                text("SELECT status, failed_stage FROM pipeline_runs WHERE message_id = :id"),
                {"id": mail_id},
            )
        ).one()
        msg_row = (
            await session.execute(
                text("SELECT folder_id, is_seen FROM messages WHERE id = :id"), {"id": mail_id},
            )
        ).one()

    assert run_row.status == "done", run_row.failed_stage
    assert msg_row.folder_id == spam_id
    assert msg_row.is_seen is True
