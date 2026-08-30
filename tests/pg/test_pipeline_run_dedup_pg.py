"""
pipeline_runs' dedup key includes the sender: a message forging the
Message-ID of one already run must get its own run, not silently
collapse into the first one -- database/models.py's PipelineRun
docstring, and 0008_pipeline_run_from_addr's own docstring, both explain
why. Reuses test_embedding_gate.py's account/folder seeding.
"""

from __future__ import annotations

import itertools
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.pipeline.enqueue import enqueue_pipeline_run_if_live_eligible
from tests.pg.test_embedding_gate import _seed_synced_account_and_inbox, _settings

_imap_uid_counter = itertools.count(1)

_FORGED_MESSAGE_ID = "<forged-campaign@attacker.example>"


async def _seed_message_with_header(
    session: AsyncSession, *, account_id: uuid.UUID, folder_id: uuid.UUID,
    from_addr: str, message_id_hdr: str,
) -> uuid.UUID:
    mail_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, from_addr, "
            "subject, body_text, received_at, size_bytes, created_at) "
            "VALUES (:id, :account_id, :folder_id, :imap_uid, :thread_id, :message_id, "
            ":from_addr, 'subject', 'Body.', now(), 1024, now())"
        ),
        {
            "id": mail_id, "account_id": account_id, "folder_id": folder_id,
            "imap_uid": next(_imap_uid_counter), "thread_id": uuid.uuid4(),
            "message_id": message_id_hdr, "from_addr": from_addr,
        },
    )
    return mail_id


@pytest.mark.asyncio
async def test_two_different_senders_reusing_a_message_id_each_get_a_run(
    migrated_db: DatabaseConnection,
) -> None:
    """
    A spammer sets one fixed Message-ID across a campaign. Before the fix,
    the second message with the same header collapsed into the first
    run's dedup key (account_id, msg_key, dedup_key) -- no sender in it --
    so it got no run at all: never embedded into a verdict, never
    classified, no trace anywhere, and the ON CONFLICT DO UPDATE
    repointed the first message's run row at the second message's id.
    """
    settings_service = await _settings(migrated_db)
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_synced_account_and_inbox(session)
        victim_mail_id = await _seed_message_with_header(
            session, account_id=account_id, folder_id=folder_id,
            from_addr="victim@example.com", message_id_hdr=_FORGED_MESSAGE_ID,
        )
        forger_mail_id = await _seed_message_with_header(
            session, account_id=account_id, folder_id=folder_id,
            from_addr="forger@attacker.example", message_id_hdr=_FORGED_MESSAGE_ID,
        )
        await session.commit()

    async with migrated_db.session() as session:
        first_inserted = await enqueue_pipeline_run_if_live_eligible(
            session, account_id=account_id, message_id=victim_mail_id,
            settings_service=settings_service,
        )
        await session.commit()
    async with migrated_db.session() as session:
        second_inserted = await enqueue_pipeline_run_if_live_eligible(
            session, account_id=account_id, message_id=forger_mail_id,
            settings_service=settings_service,
        )
        await session.commit()

    try:
        assert first_inserted is True
        assert second_inserted is True  # not silently absorbed into the first run

        async with migrated_db.session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT message_id, from_addr FROM pipeline_runs "
                        "WHERE account_id = :a"
                    ),
                    {"a": account_id},
                )
            ).all()

        assert len(rows) == 2  # one run per sender, not one shared run
        by_message_id = {row.message_id: row.from_addr for row in rows}
        assert by_message_id[victim_mail_id] == "victim@example.com"
        assert by_message_id[forger_mail_id] == "forger@attacker.example"
        # The victim's own run must still point at the victim's message --
        # the pre-fix bug repointed it at the forger's message instead.
        assert victim_mail_id in by_message_id
    finally:
        async with migrated_db.session() as session:
            await session.execute(
                text("DELETE FROM pipeline_runs WHERE account_id = :a"), {"a": account_id},
            )
            await session.commit()
