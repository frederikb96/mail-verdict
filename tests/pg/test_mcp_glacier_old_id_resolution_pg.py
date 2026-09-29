"""
The MCP surface has its own copies of the live-vs-glacier lookups
api/mails.py makes (get_mail, reply_mail) -- proven here the same way
tests/pg/test_glacier_old_id_resolution_pg.py proves the REST ones: run
against the real glacier_message_now flow, so a client's *original* id
(the one it held before the message was glaciered) keeps resolving.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastmcp import Client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.mcp_tools import mcp
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.glacier.operations import glacier_message_now
from mail_verdict.postimap.contract import read_postimap_info, supports_message_append

_TARGETS = (
    "mail_verdict.api.mcp_tools.get_db_connection",
    "mail_verdict.api.deps.get_db_connection",
    "mail_verdict.api.mails.get_db_connection",
)

_RAW_SOURCE = b"From: sender@example.com\r\nSubject: Test\r\n\r\nBody\r\n"


@pytest_asyncio.fixture()
async def mcp_client(migrated_db: DatabaseConnection) -> AsyncIterator[Client]:
    patchers = [patch(target, return_value=migrated_db) for target in _TARGETS]
    for p in patchers:
        p.start()
    try:
        async with Client(mcp) as client:
            yield client
    finally:
        for p in patchers:
            p.stop()


async def _skip_unless_append_capable(db: DatabaseConnection) -> None:
    async with db.session() as session:
        info = await read_postimap_info(session)
    if info is None or not supports_message_append(info):
        pytest.skip(
            'this PostIMAP build does not carry outbox kind="append" -- '
            f"reports service_version={info.service_version if info else 'unknown'}, "
            "so glacier_message_now is correctly refused rather than exercised here"
        )


async def _seed_ready_message(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    account_id = uuid.uuid4()
    folder_id = uuid.uuid4()
    message_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts "
            "(id, name, imap_host, imap_port, imap_user, imap_password, is_active) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'), true)"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use, initial_sync_done) "
            "VALUES (:id, :account_id, 'Archive', 'archive', true)"
        ),
        {"id": folder_id, "account_id": account_id},
    )
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
            " from_addr, body_text, raw_source, size_bytes, received_at) "
            "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :message_id_hdr, "
            " 'Findable by its old id', 'sender@example.com', 'Body text', :raw_source, "
            " :size_bytes, now())"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": folder_id,
            "thread_id": message_id, "message_id_hdr": f"<{message_id}@example.com>",
            "raw_source": _RAW_SOURCE, "size_bytes": len(_RAW_SOURCE),
        },
    )
    await session.execute(
        text(
            "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id) "
            "VALUES (:account_id, true, :glacier_folder_id)"
        ),
        {"account_id": account_id, "glacier_folder_id": uuid.uuid4()},
    )
    return account_id, message_id


@pytest.mark.asyncio
async def test_get_mail_resolves_the_original_id(
    mcp_client: Client, migrated_db: DatabaseConnection,
) -> None:
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        _account_id, message_id = await _seed_ready_message(session)
        await session.commit()

    outcome = await glacier_message_now(migrated_db, message_id)
    assert outcome.ok, outcome.reason

    result = await mcp_client.call_tool("get_mail", {"mail_id": str(message_id)})
    mail = result.data
    assert "error" not in mail, mail
    assert mail["is_glacier"] is True
    assert mail["id"] == str(outcome.glacier_id)
    assert mail["subject"] == "Findable by its old id"


@pytest.mark.asyncio
async def test_reply_mail_drafts_against_the_original_id(
    mcp_client: Client, migrated_db: DatabaseConnection,
) -> None:
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        _account_id, message_id = await _seed_ready_message(session)
        await session.commit()

    outcome = await glacier_message_now(migrated_db, message_id)
    assert outcome.ok, outcome.reason

    result = await mcp_client.call_tool(
        "reply_mail",
        {
            "mail_id": str(message_id), "mode": "reply", "body_text": "Thanks!",
            "send": False,
        },
    )
    reply = result.data
    assert reply["success"] is True, reply
