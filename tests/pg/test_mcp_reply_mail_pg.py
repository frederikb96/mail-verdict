"""
The reply_mail MCP tool against a real database, over FastMCP's own
in-memory Client -- what an agent actually calls, not the bare function.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastmcp import Client
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.mcp_tools import mcp
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Identity, Outbox, OutboxAttachment

_TARGETS = (
    "mail_verdict.api.mcp_tools.get_db_connection",
    "mail_verdict.api.deps.get_db_connection",
    "mail_verdict.api.mails.get_db_connection",
)


@pytest_asyncio.fixture()
async def mcp_client(migrated_db: DatabaseConnection) -> AsyncIterator[Client]:
    """reply_mail reads through get_message_quote (api/mails.py) and the
    attachment repository (api/deps.py) as well as mcp_tools.py's own
    module-level binding -- all three need patching."""
    patchers = [patch(target, return_value=migrated_db) for target in _TARGETS]
    for p in patchers:
        p.start()
    try:
        async with Client(mcp) as client:
            yield client
    finally:
        for p in patchers:
            p.stop()


async def _seed_message(
    session: AsyncSession,
    *,
    from_addr: str = "sender@example.com",
    to_addrs: list[str] | None = None,
    cc_addrs: list[str] | None = None,
    subject: str = "Original subject",
    body_text: str = "original body",
    body_html: str | None = None,
    message_id_header: str | None = None,
    is_truncated: bool = False,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Returns (account_id, message_id). accounts/folders/messages are
    PostIMAP-owned tables -- raw SQL, the same as every other pg test.
    The seeded account's own imap_user is me@example.com throughout, so
    from_addr="me@example.com" is how a test builds a message this
    account sent itself."""
    account_id = uuid.uuid4()
    folder_id = uuid.uuid4()
    message_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'me@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use) "
            "VALUES (:id, :account_id, 'INBOX', NULL)"
        ),
        {"id": folder_id, "account_id": account_id},
    )
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
            "from_addr, to_addrs, cc_addrs, body_text, body_html, is_truncated) "
            "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :message_id, :subject, "
            ":from_addr, :to_addrs, :cc_addrs, :body_text, :body_html, :is_truncated)"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": folder_id,
            "thread_id": uuid.uuid4(),
            "message_id": message_id_header or f"<{message_id}@example.com>",
            "subject": subject, "from_addr": from_addr,
            "to_addrs": json.dumps(to_addrs) if to_addrs is not None else None,
            "cc_addrs": json.dumps(cc_addrs) if cc_addrs is not None else None,
            "body_text": body_text, "body_html": body_html, "is_truncated": is_truncated,
        },
    )
    return account_id, message_id


async def _outbox_row(migrated_db: DatabaseConnection, outbox_id: uuid.UUID) -> Outbox:
    async with migrated_db.session() as session:
        row = await session.scalar(select(Outbox).where(Outbox.id == outbox_id))
    assert row is not None
    return row


async def _outbox_attachments(
    migrated_db: DatabaseConnection, outbox_id: uuid.UUID,
) -> list[OutboxAttachment]:
    async with migrated_db.session() as session:
        result = await session.execute(
            select(OutboxAttachment).where(OutboxAttachment.outbox_id == outbox_id)
        )
        return list(result.scalars().all())


class TestReplyMode:
    @pytest.mark.asyncio
    async def test_reply_addresses_the_sender_and_threads_and_quotes(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, message_id = await _seed_message(
                session,
                from_addr="Sender Name <sender@example.com>",
                to_addrs=["me@example.com"],
                subject="Hello",
                body_text="line one",
                message_id_header="<orig@example.com>",
            )
            await session.commit()

        result = await mcp_client.call_tool(
            "reply_mail",
            {"mail_id": str(message_id), "mode": "reply", "body_text": "my reply"},
        )
        data = result.data
        assert data["success"] is True, data

        outbox = await _outbox_row(migrated_db, uuid.UUID(data["outbox_id"]))
        assert outbox.kind == "draft"
        assert outbox.account_id == account_id
        assert outbox.to_addrs == ["sender@example.com"]
        assert outbox.subject == "Re: Hello"
        assert outbox.in_reply_to == "<orig@example.com>"
        assert outbox.msg_references == ["<orig@example.com>"]
        assert "my reply" in (outbox.body_text or "")
        assert "> line one" in (outbox.body_text or "")
        assert 'class="gmail_quote"' in (outbox.body_html or "")
        assert "line one" in (outbox.body_html or "")

        # The caller gave none of to/cc/subject -- what was actually
        # derived and used comes back in the result, since that is the
        # only way to know who a draft ends up addressed to.
        assert data["to"] == ["sender@example.com"]
        assert data["cc"] == []
        assert data["subject"] == "Re: Hello"

    @pytest.mark.asyncio
    async def test_reply_to_addresses_are_additions_not_a_replacement(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            _account_id, message_id = await _seed_message(
                session, from_addr="sender@example.com",
            )
            await session.commit()

        result = await mcp_client.call_tool(
            "reply_mail",
            {
                "mail_id": str(message_id), "mode": "reply", "body_text": "hi",
                "to": ["extra@example.com"],
            },
        )
        outbox = await _outbox_row(migrated_db, uuid.UUID(result.data["outbox_id"]))
        assert set(outbox.to_addrs or []) == {"sender@example.com", "extra@example.com"}


class TestReplyAllMode:
    @pytest.mark.asyncio
    async def test_reply_all_ccs_other_recipients_excluding_own_address(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            _account_id, message_id = await _seed_message(
                session,
                from_addr="sender@example.com",
                to_addrs=["me@example.com", "other@example.com"],
                cc_addrs=["third@example.com"],
            )
            await session.commit()

        result = await mcp_client.call_tool(
            "reply_mail",
            {"mail_id": str(message_id), "mode": "reply_all", "body_text": "hi all"},
        )
        outbox = await _outbox_row(migrated_db, uuid.UUID(result.data["outbox_id"]))
        assert outbox.to_addrs == ["sender@example.com"]
        assert set(outbox.cc_addrs or []) == {"other@example.com", "third@example.com"}
        assert "me@example.com" not in (outbox.cc_addrs or [])

    @pytest.mark.asyncio
    async def test_reply_all_to_your_own_sent_message_goes_to_its_recipients(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        """A message found in Sent (from_addr is this account's own
        imap_user) has nobody to reply "to the sender" -- Gmail's rule,
        and this tool's, is to go back to whoever it was originally sent
        to instead of to yourself."""
        async with migrated_db.session() as session:
            _account_id, message_id = await _seed_message(
                session,
                from_addr="me@example.com",
                to_addrs=["them@example.com"],
                cc_addrs=["other@example.com"],
            )
            await session.commit()

        result = await mcp_client.call_tool(
            "reply_mail",
            {"mail_id": str(message_id), "mode": "reply_all", "body_text": "hi"},
        )
        outbox = await _outbox_row(migrated_db, uuid.UUID(result.data["outbox_id"]))
        assert outbox.to_addrs == ["them@example.com"]
        assert outbox.cc_addrs == ["other@example.com"]

    @pytest.mark.asyncio
    async def test_an_address_named_in_the_callers_cc_is_not_repeated_from_to(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            _account_id, message_id = await _seed_message(session, from_addr="sender@example.com")
            await session.commit()

        result = await mcp_client.call_tool(
            "reply_mail",
            {
                "mail_id": str(message_id), "mode": "reply", "body_text": "hi",
                # sender@example.com is already the derived To -- naming
                # it again in cc must not address it twice.
                "cc": ["sender@example.com", "extra@example.com"],
            },
        )
        outbox = await _outbox_row(migrated_db, uuid.UUID(result.data["outbox_id"]))
        assert outbox.to_addrs == ["sender@example.com"]
        assert outbox.cc_addrs == ["extra@example.com"]


class TestForwardMode:
    @pytest.mark.asyncio
    async def test_forward_carries_no_default_recipients_and_a_fresh_subject(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            _account_id, message_id = await _seed_message(session, subject="Hello")
            await session.commit()

        result = await mcp_client.call_tool(
            "reply_mail",
            {
                "mail_id": str(message_id), "mode": "forward", "body_text": "fyi",
                "to": ["someone@example.com"],
            },
        )
        outbox = await _outbox_row(migrated_db, uuid.UUID(result.data["outbox_id"]))
        assert outbox.to_addrs == ["someone@example.com"]
        assert outbox.subject == "Fwd: Hello"
        assert outbox.in_reply_to is None
        assert outbox.msg_references is None

    @pytest.mark.asyncio
    async def test_forward_carries_the_originals_own_attachments_along(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            _account_id, message_id = await _seed_message(session)
            await session.execute(
                text(
                    "INSERT INTO attachments "
                    "(id, message_id, filename, content_type, size_bytes, data) "
                    "VALUES (:id, :message_id, 'report.pdf', 'application/pdf', 4, :data)"
                ),
                {"id": uuid.uuid4(), "message_id": message_id, "data": b"%PDF"},
            )
            await session.commit()

        result = await mcp_client.call_tool(
            "reply_mail",
            {
                "mail_id": str(message_id), "mode": "forward", "body_text": "fyi",
                "to": ["someone@example.com"],
            },
        )
        attachments = await _outbox_attachments(migrated_db, uuid.UUID(result.data["outbox_id"]))
        assert len(attachments) == 1
        assert attachments[0].filename == "report.pdf"
        assert attachments[0].data == b"%PDF"


class TestReplyMailIdentityAndSendAttachments:
    @pytest.mark.asyncio
    async def test_the_identity_the_original_was_addressed_to_is_preferred(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            account_id, message_id = await _seed_message(
                session, to_addrs=["alias@example.com"],
            )
            session.add(
                Identity(account_id=account_id, email="default@example.com", is_default=True)
            )
            session.add(
                Identity(account_id=account_id, email="alias@example.com", is_default=False)
            )
            await session.commit()

        result = await mcp_client.call_tool(
            "reply_mail",
            {"mail_id": str(message_id), "mode": "reply", "body_text": "hi"},
        )
        outbox = await _outbox_row(migrated_db, uuid.UUID(result.data["outbox_id"]))
        assert outbox.from_addr == "alias@example.com"

    @pytest.mark.asyncio
    async def test_a_caller_supplied_attachment_is_decoded_and_attached(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            _account_id, message_id = await _seed_message(session)
            await session.commit()

        payload = base64.b64encode(b"hello file").decode()
        result = await mcp_client.call_tool(
            "reply_mail",
            {
                "mail_id": str(message_id), "mode": "reply", "body_text": "see attached",
                "attachments": [
                    {"filename": "note.txt", "content_type": "text/plain", "data_base64": payload},
                ],
            },
        )
        attachments = await _outbox_attachments(migrated_db, uuid.UUID(result.data["outbox_id"]))
        assert len(attachments) == 1
        assert attachments[0].filename == "note.txt"
        assert attachments[0].data == b"hello file"

    @pytest.mark.asyncio
    async def test_send_true_inserts_a_send_row(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            _account_id, message_id = await _seed_message(
                session, from_addr="sender@example.com",
            )
            await session.commit()

        result = await mcp_client.call_tool(
            "reply_mail",
            {
                "mail_id": str(message_id), "mode": "reply", "body_text": "hi",
                "send": True,
            },
        )
        outbox = await _outbox_row(migrated_db, uuid.UUID(result.data["outbox_id"]))
        assert outbox.kind == "send"


class TestReplyMailErrors:
    @pytest.mark.asyncio
    async def test_an_unknown_mode_is_refused(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            _account_id, message_id = await _seed_message(session)
            await session.commit()

        result = await mcp_client.call_tool(
            "reply_mail",
            {"mail_id": str(message_id), "mode": "reply-all", "body_text": "hi"},
        )
        assert result.data["success"] is False
        assert "mode" in result.data["error"]

    @pytest.mark.asyncio
    async def test_an_unknown_message_is_reported_not_raised(
        self, mcp_client: Client,
    ) -> None:
        result = await mcp_client.call_tool(
            "reply_mail",
            {"mail_id": str(uuid.uuid4()), "mode": "reply", "body_text": "hi"},
        )
        assert result.data["success"] is False
        assert "not found" in result.data["error"].lower()

    @pytest.mark.asyncio
    async def test_a_malformed_mail_id_is_reported_not_raised(
        self, mcp_client: Client,
    ) -> None:
        result = await mcp_client.call_tool(
            "reply_mail",
            {"mail_id": "not-a-uuid", "mode": "reply", "body_text": "hi"},
        )
        assert result.data["success"] is False
        assert "error" in result.data

    @pytest.mark.asyncio
    async def test_a_malformed_identity_id_is_reported_not_raised(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            _account_id, message_id = await _seed_message(session)
            await session.commit()

        result = await mcp_client.call_tool(
            "reply_mail",
            {
                "mail_id": str(message_id), "mode": "reply", "body_text": "hi",
                "identity_id": "not-a-uuid",
            },
        )
        assert result.data["success"] is False
        assert "error" in result.data

    @pytest.mark.asyncio
    async def test_a_foreign_identity_id_is_reported_not_raised(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        """resolve_send_from_addr raises HTTPException(400) for an
        identity that exists but belongs to a different account."""
        async with migrated_db.session() as session:
            _account_id, message_id = await _seed_message(session)
            other_account_id, _other_message_id = await _seed_message(session)
            foreign_identity = Identity(account_id=other_account_id, email="x@example.com")
            session.add(foreign_identity)
            await session.commit()
            foreign_identity_id = foreign_identity.id

        result = await mcp_client.call_tool(
            "reply_mail",
            {
                "mail_id": str(message_id), "mode": "reply", "body_text": "hi",
                "identity_id": str(foreign_identity_id),
            },
        )
        assert result.data["success"] is False
        assert "error" in result.data

    @pytest.mark.asyncio
    async def test_forwarding_with_send_and_no_recipient_is_reported_not_raised(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        """forward derives no recipients of its own; require_recipients
        (called for kind="send") used to raise HTTPException straight
        through this tool rather than answering with the documented
        error shape."""
        async with migrated_db.session() as session:
            _account_id, message_id = await _seed_message(session)
            await session.commit()

        result = await mcp_client.call_tool(
            "reply_mail",
            {
                "mail_id": str(message_id), "mode": "forward", "body_text": "fyi",
                "send": True,
            },
        )
        assert result.data["success"] is False
        assert "error" in result.data


class TestTruncatedSourceMessage:
    """Per the consumer contract, is_truncated means body_text/body_html
    and every attachment were never fetched and stay NULL/empty forever
    -- replying would quote nothing, and forwarding would silently carry
    along zero-byte attachments while reporting success."""

    @pytest.mark.asyncio
    async def test_replying_to_a_truncated_message_is_refused(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            _account_id, message_id = await _seed_message(session, is_truncated=True)
            await session.commit()

        result = await mcp_client.call_tool(
            "reply_mail",
            {"mail_id": str(message_id), "mode": "reply", "body_text": "hi"},
        )
        assert result.data["success"] is False
        assert "truncated" in result.data["error"].lower()

    @pytest.mark.asyncio
    async def test_forwarding_a_truncated_message_is_refused(
        self, mcp_client: Client, migrated_db: DatabaseConnection,
    ) -> None:
        async with migrated_db.session() as session:
            _account_id, message_id = await _seed_message(session, is_truncated=True)
            await session.commit()

        result = await mcp_client.call_tool(
            "reply_mail",
            {
                "mail_id": str(message_id), "mode": "forward", "body_text": "fyi",
                "to": ["someone@example.com"],
            },
        )
        assert result.data["success"] is False
        assert "truncated" in result.data["error"].lower()
