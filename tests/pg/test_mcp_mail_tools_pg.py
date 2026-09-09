"""
An MCP client's get_mail tool, against a real database, over FastMCP's
own in-memory Client rather than calling the underlying function
directly -- these tools are what an agent actually calls.
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

_TARGETS = (
    "mail_verdict.api.mcp_tools.get_db_connection",
    "mail_verdict.api.deps.get_db_connection",
)


@pytest_asyncio.fixture()
async def mcp_client(migrated_db: DatabaseConnection) -> AsyncIterator[Client]:
    """get_mail resolves the attachment repository through api/deps.py,
    a second module-level binding of get_db_connection distinct from
    mcp_tools.py's own -- both need patching."""
    patchers = [patch(target, return_value=migrated_db) for target in _TARGETS]
    for p in patchers:
        p.start()
    try:
        async with Client(mcp) as client:
            yield client
    finally:
        for p in patchers:
            p.stop()


async def _seed_message_with_attachment(session: AsyncSession) -> uuid.UUID:
    account_id = uuid.uuid4()
    inbox_id = uuid.uuid4()
    message_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use) "
            "VALUES (:id, :account_id, 'INBOX', NULL)"
        ),
        {"id": inbox_id, "account_id": account_id},
    )
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject) "
            "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :msg_id, 'Test message')"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": inbox_id,
            "thread_id": message_id, "msg_id": f"<{message_id}@example.com>",
        },
    )
    await session.execute(
        text(
            "INSERT INTO attachments (id, message_id, filename, content_type, size_bytes, data) "
            "VALUES (:id, :message_id, 'report.pdf', 'application/pdf', 4, :data)"
        ),
        {"id": uuid.uuid4(), "message_id": message_id, "data": b"%PDF"},
    )
    return message_id


class TestGetMailTool:
    @pytest.mark.asyncio
    async def test_get_mail_lists_the_messages_attachments(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        """The regression this guards: get_mail built its response from
        the Message row alone, with no attachment lookup at all -- an
        agent had no way to even learn a message had one."""
        async with migrated_db.session() as session:
            message_id = await _seed_message_with_attachment(session)
            await session.commit()

        result = await mcp_client.call_tool("get_mail", {"mail_id": str(message_id)})
        mail = result.data
        assert "error" not in mail, mail
        assert len(mail["attachments"]) == 1
        assert mail["attachments"][0]["filename"] == "report.pdf"
        assert mail["attachments"][0]["content_type"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_get_mail_on_a_message_with_no_attachments_returns_an_empty_list(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id = uuid.uuid4()
            inbox_id = uuid.uuid4()
            message_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, name, imap_host, imap_port, imap_user, imap_password) "
                    "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
                    "'\\x00' || convert_to('pw', 'UTF8'))"
                ),
                {"id": account_id, "name": f"acct-{account_id}"},
            )
            await session.execute(
                text(
                    "INSERT INTO folders (id, account_id, imap_name, special_use) "
                    "VALUES (:id, :account_id, 'INBOX', NULL)"
                ),
                {"id": inbox_id, "account_id": account_id},
            )
            await session.execute(
                text(
                    "INSERT INTO messages "
                    "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject) "
                    "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :msg_id, "
                    "'No attachments')"
                ),
                {
                    "id": message_id, "account_id": account_id, "folder_id": inbox_id,
                    "thread_id": message_id, "msg_id": f"<{message_id}@example.com>",
                },
            )
            await session.commit()

        result = await mcp_client.call_tool("get_mail", {"mail_id": str(message_id)})
        assert result.data["attachments"] == []
