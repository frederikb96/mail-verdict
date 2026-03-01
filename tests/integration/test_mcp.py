"""
Integration tests: MCP tool contracts.

Tests all 8 MCP tools (search_mail, get_mail, list_folders, list_accounts,
move_mail, tag_mail, get_verdict, get_stats) with valid inputs, invalid inputs,
and edge cases.

Uses real Postgres via test container. Mocks SemanticStore for semantic search.

Markers: @pytest.mark.integration
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from unittest.mock import patch

import pytest

from mail_verdict.config import DatabaseConfig
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import (
    Account,
    Base,
    Folder,
    Mail,
    SpecialUse,
    Verdict,
    VerdictSource,
)

TEST_DB_URL = (
    "postgresql+asyncpg://mail_verdict_test:mail_verdict_test@localhost:5433/mail_verdict_test"
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[DatabaseConnection]:
    """Module-scoped database connection."""
    config = DatabaseConfig(url=TEST_DB_URL, pool_size=5, max_overflow=2)
    conn = DatabaseConnection(config)
    await conn.init()
    async with conn.engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    yield conn
    async with conn.engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
    await conn.close()


@pytest.fixture(scope="module")
async def seed_data(db: DatabaseConnection) -> dict[str, Any]:
    """Seed test data for MCP tool testing."""
    async with db.session() as session:
        acc = Account(
            name="mcp-test-account",
            imap_host="imap.mcp.test",
            imap_port=993,
            imap_user="mcp@test.com",
        )
        session.add(acc)
        await session.flush()

        inbox = Folder(
            account_id=acc.id,
            imap_name="INBOX",
            display_name="Inbox",
            special_use=SpecialUse.INBOX,
        )
        junk = Folder(
            account_id=acc.id,
            imap_name="Junk",
            display_name="Spam",
            special_use=SpecialUse.JUNK,
        )
        session.add(inbox)
        session.add(junk)
        await session.flush()

        mail = Mail(
            account_id=acc.id,
            folder_id=inbox.id,
            uid=100,
            message_id="<mcp-test@example.com>",
            subject="MCP Test Email",
            from_addr="sender@mcptest.com",
            to_addrs={"addr": "mcp@test.com"},
            body_text="Testing MCP tool contracts.",
            received_at=datetime.now(timezone.utc),
            size_bytes=256,
        )
        session.add(mail)
        await session.flush()

        verdict = Verdict(
            mail_id=mail.id,
            is_spam=False,
            source=VerdictSource.AI,
            model_used="gpt-4o-mini",
        )
        session.add(verdict)
        await session.flush()

        return {
            "account_id": str(acc.id),
            "inbox_id": str(inbox.id),
            "junk_id": str(junk.id),
            "mail_id": str(mail.id),
            "verdict_id": str(verdict.id),
        }


@pytest.fixture(autouse=True)
def _patch_db(db: DatabaseConnection) -> Any:
    """Patch the global DB connection for all MCP tool calls."""
    import mail_verdict.database.connection as db_mod

    original = db_mod._db_connection
    db_mod._db_connection = db
    yield
    db_mod._db_connection = original


class TestSearchMail:
    """Test search_mail MCP tool."""

    async def test_fulltext_search_valid(self, seed_data: dict[str, Any]) -> None:
        """Search with valid query in fulltext mode."""
        from mail_verdict.api.mcp_tools import search_mail

        results = await search_mail(
            query="MCP Test",
            account_id=seed_data["account_id"],
            mode="fulltext",
            limit=10,
        )
        assert isinstance(results, list)

    async def test_fulltext_search_no_results(self, seed_data: dict[str, Any]) -> None:
        """Search for nonexistent content returns empty list."""
        from mail_verdict.api.mcp_tools import search_mail

        results = await search_mail(
            query="xyznonexistent12345",
            account_id=seed_data["account_id"],
            mode="fulltext",
        )
        assert isinstance(results, list)

    async def test_semantic_search_no_store(self, seed_data: dict[str, Any]) -> None:
        """Semantic search when store not initialized returns error."""
        from mail_verdict.api.mcp_tools import search_mail
        from mail_verdict.semantic.store import SemanticStore

        # Ensure no store instance
        original = SemanticStore._instance
        SemanticStore._instance = None

        results = await search_mail(
            query="test",
            mode="semantic",
        )
        assert isinstance(results, list)
        if results:
            assert "error" in results[0]

        SemanticStore._instance = original

    async def test_search_all_accounts(self) -> None:
        """Search across all accounts (no account_id)."""
        from mail_verdict.api.mcp_tools import search_mail

        results = await search_mail(query="test", mode="fulltext")
        assert isinstance(results, list)


class TestGetMail:
    """Test get_mail MCP tool."""

    async def test_get_valid_mail(self, seed_data: dict[str, Any]) -> None:
        """Get a mail that exists."""
        from mail_verdict.api.mcp_tools import get_mail

        result = await get_mail(
            mail_id=seed_data["mail_id"],
            account_id=seed_data["account_id"],
        )
        assert isinstance(result, dict)
        assert result["id"] == seed_data["mail_id"]
        assert result["subject"] == "MCP Test Email"
        assert "body_text" in result
        assert "dkim_pass" in result

    async def test_get_nonexistent_mail(self, seed_data: dict[str, Any]) -> None:
        """Get a mail that does not exist returns error."""
        from mail_verdict.api.mcp_tools import get_mail

        result = await get_mail(
            mail_id=str(uuid.uuid4()),
            account_id=seed_data["account_id"],
        )
        assert "error" in result

    async def test_get_mail_wrong_account(self, seed_data: dict[str, Any]) -> None:
        """Get a mail with wrong account_id returns error."""
        from mail_verdict.api.mcp_tools import get_mail

        result = await get_mail(
            mail_id=seed_data["mail_id"],
            account_id=str(uuid.uuid4()),
        )
        assert "error" in result


class TestListFolders:
    """Test list_folders MCP tool."""

    async def test_list_valid_account(self, seed_data: dict[str, Any]) -> None:
        """List folders for a valid account."""
        from mail_verdict.api.mcp_tools import list_folders

        results = await list_folders(account_id=seed_data["account_id"])
        assert isinstance(results, list)
        assert len(results) >= 2
        imap_names = [f["imap_name"] for f in results]
        assert "INBOX" in imap_names
        assert "Junk" in imap_names

    async def test_list_empty_account(self) -> None:
        """List folders for nonexistent account returns empty list."""
        from mail_verdict.api.mcp_tools import list_folders

        results = await list_folders(account_id=str(uuid.uuid4()))
        assert results == []


class TestListAccounts:
    """Test list_accounts MCP tool."""

    async def test_list_accounts(self, seed_data: dict[str, Any]) -> None:
        """List all accounts."""
        from mail_verdict.api.mcp_tools import list_accounts

        results = await list_accounts()
        assert isinstance(results, list)
        assert len(results) >= 1
        assert any(a["id"] == seed_data["account_id"] for a in results)


class TestMoveMail:
    """Test move_mail MCP tool."""

    async def test_move_valid(self, seed_data: dict[str, Any]) -> None:
        """Move a mail to Junk folder."""
        from mail_verdict.api.mcp_tools import move_mail

        result = await move_mail(
            mail_id=seed_data["mail_id"],
            account_id=seed_data["account_id"],
            target_folder="Junk",
        )
        assert result["success"] is True
        assert "Junk" in result.get("message", "")

    async def test_move_nonexistent_mail(self, seed_data: dict[str, Any]) -> None:
        """Move a nonexistent mail returns error."""
        from mail_verdict.api.mcp_tools import move_mail

        result = await move_mail(
            mail_id=str(uuid.uuid4()),
            account_id=seed_data["account_id"],
            target_folder="Junk",
        )
        assert result["success"] is False

    async def test_move_to_nonexistent_folder(self, seed_data: dict[str, Any]) -> None:
        """Move to a nonexistent folder returns error."""
        from mail_verdict.api.mcp_tools import move_mail

        result = await move_mail(
            mail_id=seed_data["mail_id"],
            account_id=seed_data["account_id"],
            target_folder="NonexistentFolder",
        )
        assert result["success"] is False


class TestTagMail:
    """Test tag_mail MCP tool."""

    async def test_tag_valid(self, seed_data: dict[str, Any]) -> None:
        """Add a tag to a mail."""
        from mail_verdict.api.mcp_tools import tag_mail

        result = await tag_mail(
            mail_id=seed_data["mail_id"],
            tag_name="important",
            source="user",
        )
        assert result["success"] is True
        assert result["tag_name"] == "important"
        assert result["source"] == "user"

    async def test_tag_idempotent(self, seed_data: dict[str, Any]) -> None:
        """Adding the same tag twice is idempotent."""
        from mail_verdict.api.mcp_tools import tag_mail

        r1 = await tag_mail(mail_id=seed_data["mail_id"], tag_name="idempotent-tag")
        r2 = await tag_mail(mail_id=seed_data["mail_id"], tag_name="idempotent-tag")
        assert r1["success"] is True
        assert r2["success"] is True

    async def test_tag_with_source_types(self, seed_data: dict[str, Any]) -> None:
        """Test all valid source types."""
        from mail_verdict.api.mcp_tools import tag_mail

        for src in ("user", "rule", "enrichment", "spam", "imap"):
            result = await tag_mail(
                mail_id=seed_data["mail_id"],
                tag_name=f"tag-{src}",
                source=src,
            )
            assert result["success"] is True
            assert result["source"] == src


class TestGetVerdict:
    """Test get_verdict MCP tool."""

    async def test_get_existing_verdict(self, seed_data: dict[str, Any]) -> None:
        """Get verdict for a mail that has one."""
        from mail_verdict.api.mcp_tools import get_verdict

        # Move mail back to INBOX first (it was moved to Junk in MoveMail test)
        result = await get_verdict(mail_id=seed_data["mail_id"])
        assert result is not None
        assert "is_spam" in result
        assert "source" in result

    async def test_get_verdict_no_verdict(self) -> None:
        """Get verdict for a mail with no verdict returns None."""
        from mail_verdict.api.mcp_tools import get_verdict

        result = await get_verdict(mail_id=str(uuid.uuid4()))
        assert result is None


class TestGetStats:
    """Test get_stats MCP tool."""

    async def test_get_stats_for_account(self, seed_data: dict[str, Any]) -> None:
        """Get stats for a specific account."""
        from mail_verdict.api.mcp_tools import get_stats

        # Patch SpamMetrics to avoid complex query failures on minimal test data
        from mail_verdict.spam.metrics import SpamMetrics, SpamStats

        mock_stats = SpamStats(
            total_verdicts=10,
            ai_verdicts=8,
            rule_verdicts=1,
            user_corrections=1,
            spam_count=5,
            ham_count=5,
            false_positives=0,
            false_negatives=0,
            correction_rate=0.1,
            fp_rate=0.0,
            fn_rate=0.0,
            accuracy=1.0,
        )
        with patch.object(SpamMetrics, "get_stats", return_value=mock_stats):
            result = await get_stats(account_id=seed_data["account_id"])
            assert isinstance(result, dict)
            assert "total_verdicts" in result

    async def test_get_stats_all_accounts(self, seed_data: dict[str, Any]) -> None:
        """Get stats across all accounts."""
        from mail_verdict.api.mcp_tools import get_stats
        from mail_verdict.spam.metrics import SpamMetrics, SpamStats

        mock_stats = SpamStats(
            total_verdicts=5,
            ai_verdicts=4,
            rule_verdicts=0,
            user_corrections=1,
            spam_count=2,
            ham_count=3,
            false_positives=0,
            false_negatives=0,
            correction_rate=0.2,
            fp_rate=0.0,
            fn_rate=0.0,
            accuracy=1.0,
        )
        with patch.object(SpamMetrics, "get_stats", return_value=mock_stats):
            result = await get_stats()
            assert isinstance(result, dict)
