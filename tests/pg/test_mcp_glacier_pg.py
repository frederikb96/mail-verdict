"""
The MCP tools against a glaciered message (design section 4.8): get_mail,
get_thread, list_mails, search_mail, mark_mail and reply_mail all read
and write glacier_messages the same way their api/mails.py counterparts
already do -- proven here through FastMCP's own in-memory Client, the
same way an agent actually calls them.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastmcp import Client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.mcp_tools import mcp
from mail_verdict.database.connection import DatabaseConnection

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


async def _seed_account(session: AsyncSession) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts "
            "(id, name, imap_host, imap_port, imap_user, imap_password, is_active) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'), false)"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    return account_id


async def _enable_glacier(session: AsyncSession, account_id: uuid.UUID) -> uuid.UUID:
    glacier_folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id) "
            "VALUES (:account_id, true, :glacier_folder_id)"
        ),
        {"account_id": account_id, "glacier_folder_id": glacier_folder_id},
    )
    return glacier_folder_id


async def _seed_glacier_message(
    session: AsyncSession, *, account_id: uuid.UUID, glacier_folder_id: uuid.UUID,
    subject: str = "Archived subject", from_addr: str = "sender@example.com",
    body_text: str = "Archived body", received_at: datetime | None = None,
    thread_id: uuid.UUID | None = None, is_seen: bool = False,
) -> uuid.UUID:
    glacier_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO glacier_messages "
            "(id, account_id, folder_id, thread_id, message_id, subject, from_addr, "
            " to_addrs, body_text, raw_source, size_bytes, received_at, is_seen, "
            " msg_key, state, visible_at, glaciered_at) "
            "VALUES (:id, :account_id, :folder_id, :thread_id, :message_id_hdr, :subject, "
            " :from_addr, '[\"me@example.com\"]', :body_text, :raw_source, :size_bytes, "
            " :received_at, :is_seen, :msg_key, 'glaciered', now(), now())"
        ),
        {
            "id": glacier_id, "account_id": account_id, "folder_id": glacier_folder_id,
            "thread_id": thread_id or glacier_id, "message_id_hdr": f"<{glacier_id}@example.com>",
            "subject": subject, "from_addr": from_addr, "body_text": body_text,
            "raw_source": _RAW_SOURCE, "size_bytes": len(_RAW_SOURCE),
            "received_at": received_at or datetime.now(timezone.utc), "is_seen": is_seen,
            "msg_key": f"msg-{glacier_id}",
        },
    )
    return glacier_id


class TestGetMailOnAGlacierId:
    @pytest.mark.asyncio
    async def test_get_mail_reads_a_glaciered_message(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            glacier_folder_id = await _enable_glacier(session, account_id)
            glacier_id = await _seed_glacier_message(
                session, account_id=account_id, glacier_folder_id=glacier_folder_id,
                subject="Findable subject",
            )
            await session.commit()

        result = await mcp_client.call_tool("get_mail", {"mail_id": str(glacier_id)})
        mail = result.data
        assert "error" not in mail, mail
        assert mail["subject"] == "Findable subject"
        assert mail["is_glacier"] is True
        assert mail["pending_sync"] is False


class TestGetThreadAcrossBothTables:
    @pytest.mark.asyncio
    async def test_get_thread_includes_a_glaciered_and_a_live_message(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        now = datetime.now(timezone.utc)
        thread_id = uuid.uuid4()
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            glacier_folder_id = await _enable_glacier(session, account_id)
            live_folder_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO folders (id, account_id, imap_name, special_use, "
                    "initial_sync_done) VALUES (:id, :account_id, 'INBOX', NULL, true)"
                ),
                {"id": live_folder_id, "account_id": account_id},
            )
            newest_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO messages "
                    "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
                    " from_addr, raw_source, size_bytes, received_at) "
                    "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :msg_id, 'Re: Thread', "
                    " 'sender@example.com', :raw_source, :size_bytes, :received_at)"
                ),
                {
                    "id": newest_id, "account_id": account_id, "folder_id": live_folder_id,
                    "thread_id": thread_id, "msg_id": f"<{newest_id}@example.com>",
                    "raw_source": _RAW_SOURCE, "size_bytes": len(_RAW_SOURCE),
                    "received_at": now,
                },
            )
            glacier_id = await _seed_glacier_message(
                session, account_id=account_id, glacier_folder_id=glacier_folder_id,
                subject="Thread", received_at=now - timedelta(days=1), thread_id=thread_id,
            )
            await session.commit()

        result = await mcp_client.call_tool("get_thread", {"mail_id": str(newest_id)})
        messages = result.data
        ids = {m["id"] for m in messages}
        assert ids == {str(newest_id), str(glacier_id)}
        glacier_entry = next(m for m in messages if m["id"] == str(glacier_id))
        assert glacier_entry["is_glacier"] is True


class TestListMailsOnTheGlacierFolder:
    @pytest.mark.asyncio
    async def test_list_mails_scoped_to_the_glacier_folder(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            glacier_folder_id = await _enable_glacier(session, account_id)
            glacier_id = await _seed_glacier_message(
                session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            )
            await session.commit()

        result = await mcp_client.call_tool(
            "list_mails",
            {"account_id": str(account_id), "folder_id": str(glacier_folder_id)},
        )
        messages = result.data
        assert [m["id"] for m in messages] == [str(glacier_id)]
        assert messages[0]["is_glacier"] is True


class TestSearchMailFindsAGlacieredMessage:
    @pytest.mark.asyncio
    async def test_search_mail_finds_it_by_subject(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            glacier_folder_id = await _enable_glacier(session, account_id)
            glacier_id = await _seed_glacier_message(
                session, account_id=account_id, glacier_folder_id=glacier_folder_id,
                subject="Unique needle zzyzxsearch",
            )
            await session.commit()

        result = await mcp_client.call_tool(
            "search_mail", {"query": "zzyzxsearch", "account_id": str(account_id)},
        )
        hits = result.data
        assert any(h["id"] == str(glacier_id) and h["is_glacier"] for h in hits)


class TestMarkMailOnAGlacierId:
    @pytest.mark.asyncio
    async def test_mark_mail_sets_flags_on_a_glaciered_message(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            glacier_folder_id = await _enable_glacier(session, account_id)
            glacier_id = await _seed_glacier_message(
                session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            )
            await session.commit()

        result = await mcp_client.call_tool(
            "mark_mail", {"mail_id": str(glacier_id), "is_seen": True, "is_flagged": True},
        )
        assert result.data["success"] is True

        async with migrated_db.session() as session:
            row = (
                await session.execute(
                    text("SELECT is_seen, is_flagged FROM glacier_messages WHERE id = :id"),
                    {"id": glacier_id},
                )
            ).mappings().one()
        assert row["is_seen"] is True
        assert row["is_flagged"] is True


class TestReplyMailToAGlacieredMessage:
    @pytest.mark.asyncio
    async def test_reply_mail_drafts_a_reply_to_a_glaciered_message(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id = await _seed_account(session)
            glacier_folder_id = await _enable_glacier(session, account_id)
            glacier_id = await _seed_glacier_message(
                session, account_id=account_id, glacier_folder_id=glacier_folder_id,
                subject="Original subject", from_addr="them@example.com",
            )
            await session.commit()

        result = await mcp_client.call_tool(
            "reply_mail",
            {"mail_id": str(glacier_id), "mode": "reply", "body_text": "Thanks!"},
        )
        outcome = result.data
        assert outcome.get("success") is True, outcome
        assert outcome["to"] == ["them@example.com"]
        assert outcome["subject"] == "Re: Original subject"
