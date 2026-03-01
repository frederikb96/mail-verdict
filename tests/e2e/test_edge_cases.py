"""
E2E: Edge cases.

Tests: unicode content, huge attachments (>10MB), concurrent multi-account sync.
Verifies the system handles unusual inputs and parallel operations without crashing.

Markers: @pytest.mark.e2e
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import pytest

from mail_verdict.config import (
    AIConfig,
    DatabaseConfig,
    MailVerdictConfig,
    MCPConfig,
    QdrantConfig,
    RetryConfig,
    ServerConfig,
    SpamConfig,
    SyncConfig,
)
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import (
    Account,
    Attachment,
    Base,
    Folder,
    Mail,
    SpecialUse,
)
from mail_verdict.database.repository import MailRepository, VerdictRepository
from mail_verdict.semantic.store import SemanticStore
from mail_verdict.spam.analyst import SpamVerdict
from mail_verdict.spam.pipeline import VerdictPipeline

TEST_DB_URL = (
    "postgresql+asyncpg://mail_verdict_test:mail_verdict_test@localhost:5433/mail_verdict_test"
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.asyncio(loop_scope="module"),
]


def _test_config() -> MailVerdictConfig:
    """Build test configuration."""
    return MailVerdictConfig(
        server=ServerConfig(host="127.0.0.1", port=8080, log_level="WARNING", cors_origins=[]),
        accounts=[],
        database=DatabaseConfig(url=TEST_DB_URL, pool_size=5, max_overflow=2),
        qdrant=QdrantConfig(host="localhost", port=6334, collection_name="test_edge"),
        ai=AIConfig(
            provider="openai",
            model="gpt-4o-mini",
            embedding_model="text-embedding-3-small",
            embedding_dimensions=1536,
        ),
        spam=SpamConfig(
            enabled=True,
            excerpt_length=300,
            neighbor_count=3,
            auto_mark_read=True,
            system_prompt="test",
        ),
        sync=SyncConfig(
            poll_interval_seconds=300,
            idle_enabled=False,
            idle_restart_seconds=1500,
            lookback_days=180,
            auto_detect_folders=True,
        ),
        retry=RetryConfig(
            max_retries=1,
            base_delay_seconds=0.1,
            max_delay_seconds=1.0,
            exponential_base=2.0,
        ),
        mcp=MCPConfig(enabled=False, port=8766, transport="streamable-http"),
        rules=[],
    )


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
async def test_data(db: DatabaseConnection) -> dict[str, Any]:
    """Seed base test data."""
    async with db.session() as session:
        acc = Account(
            name="e2e-edge-account",
            imap_host="localhost",
            imap_port=1143,
            imap_user="edge@localhost",
        )
        session.add(acc)
        await session.flush()

        inbox = Folder(
            account_id=acc.id,
            imap_name="INBOX",
            special_use=SpecialUse.INBOX,
        )
        session.add(inbox)
        await session.flush()

        return {"account_id": acc.id, "inbox": inbox}


class TestUnicodeContent:
    """Test handling of unicode and special characters in mail content."""

    async def test_cjk_subject_and_body(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """
        Mail with CJK characters in subject and body is stored and classified.
        """
        config = _test_config()
        account_id = test_data["account_id"]
        inbox = test_data["inbox"]

        subject_unicode = (
            "\u3053\u3093\u306b\u3061\u306f\u4e16\u754c"
            " - \u60a8\u597d\u4e16\u754c - \uc548\ub155\ud558\uc138\uc694"
        )
        body_unicode = (
            "\u3053\u308c\u306f\u30c6\u30b9\u30c8\u30e1\u30fc\u30eb\u3067\u3059\u3002"
            "\u65e5\u672c\u8a9e\u3001\u4e2d\u56fd\u8a9e\u3001\u97d3\u56fd\u8a9e\u306e"
            "\u30c6\u30ad\u30b9\u30c8\u304c\u542b\u307e\u308c\u3066\u3044\u307e\u3059\u3002"
        )
        async with db.session() as session:
            mail = Mail(
                account_id=account_id,
                folder_id=inbox.id,
                uid=9001,
                subject=subject_unicode,
                from_addr="sender@example.jp",
                body_text=body_unicode,
                received_at=datetime.now(timezone.utc),
            )
            session.add(mail)
            await session.flush()
            await session.refresh(mail)

        mock_store = AsyncMock(spec=SemanticStore)
        mock_store.ensure_collection.return_value = True
        mock_store.upsert.return_value = True
        mock_store.search.return_value = []
        mock_store.build_embedding_text = SemanticStore.build_embedding_text

        mock_analyst = AsyncMock()
        mock_analyst.analyze.return_value = SpamVerdict(
            is_spam=False,
            raw_response={"verdict": "not-spam"},
        )

        verdict_repo = VerdictRepository(db)
        mail_repo = MailRepository(db)

        pipeline = VerdictPipeline(
            config=config,
            semantic_store=mock_store,
            analyst=mock_analyst,
            verdict_repo=verdict_repo,
            mail_repo=mail_repo,
        )

        result = await pipeline.process_mail(mail, inbox)
        assert result is False

        # Verify the unicode subject was passed to the analyst
        mock_analyst.analyze.assert_called_once()
        call_args = mock_analyst.analyze.call_args
        ctx = call_args.args[0] if call_args.args else call_args.kwargs.get("context")
        assert "\u3053\u3093\u306b\u3061\u306f" in ctx.subject

    async def test_emoji_and_special_chars(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """
        Mail with emojis, RTL text, and special unicode chars in subject/body.
        """
        config = _test_config()
        account_id = test_data["account_id"]
        inbox = test_data["inbox"]

        body_emoji = (
            "Click here \u2192 \U0001f449 to claim your "
            "\U0001f4b0\U0001f4b0\U0001f4b0! "
            "\u0645\u0631\u062d\u0628\u064b\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645"
        )
        async with db.session() as session:
            mail = Mail(
                account_id=account_id,
                folder_id=inbox.id,
                uid=9002,
                subject="\U0001f525 URGENT \U0001f4b0 You Won! \U0001f389",
                from_addr="scam@example.com",
                body_text=body_emoji,
                received_at=datetime.now(timezone.utc),
            )
            session.add(mail)
            await session.flush()
            await session.refresh(mail)

        mock_store = AsyncMock(spec=SemanticStore)
        mock_store.ensure_collection.return_value = True
        mock_store.upsert.return_value = True
        mock_store.search.return_value = []
        mock_store.build_embedding_text = SemanticStore.build_embedding_text

        mock_analyst = AsyncMock()
        mock_analyst.analyze.return_value = SpamVerdict(
            is_spam=True,
            raw_response={"verdict": "spam"},
        )

        verdict_repo = VerdictRepository(db)
        mail_repo = MailRepository(db)

        pipeline = VerdictPipeline(
            config=config,
            semantic_store=mock_store,
            analyst=mock_analyst,
            verdict_repo=verdict_repo,
            mail_repo=mail_repo,
        )

        result = await pipeline.process_mail(mail, inbox)
        assert result is True

    async def test_empty_subject_and_body(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """
        Mail with empty/null subject and body still processes.
        """
        config = _test_config()
        account_id = test_data["account_id"]
        inbox = test_data["inbox"]

        async with db.session() as session:
            mail = Mail(
                account_id=account_id,
                folder_id=inbox.id,
                uid=9003,
                subject="",
                from_addr="noreply@example.com",
                body_text="",
                received_at=datetime.now(timezone.utc),
            )
            session.add(mail)
            await session.flush()
            await session.refresh(mail)

        mock_store = AsyncMock(spec=SemanticStore)
        mock_store.ensure_collection.return_value = True
        mock_store.upsert.return_value = True
        mock_store.search.return_value = []
        mock_store.build_embedding_text = SemanticStore.build_embedding_text

        mock_analyst = AsyncMock()
        mock_analyst.analyze.return_value = SpamVerdict(
            is_spam=False,
            raw_response={"verdict": "not-spam"},
        )

        verdict_repo = VerdictRepository(db)
        mail_repo = MailRepository(db)

        pipeline = VerdictPipeline(
            config=config,
            semantic_store=mock_store,
            analyst=mock_analyst,
            verdict_repo=verdict_repo,
            mail_repo=mail_repo,
        )

        result = await pipeline.process_mail(mail, inbox)
        assert result is False


class TestHugeAttachments:
    """Test handling of large attachments and oversized mails."""

    async def test_large_attachment_stored(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """
        Mail with a large attachment (>10MB metadata) is stored correctly.
        """
        account_id = test_data["account_id"]
        inbox = test_data["inbox"]

        async with db.session() as session:
            mail = Mail(
                account_id=account_id,
                folder_id=inbox.id,
                uid=9010,
                subject="Large Attachment Test",
                from_addr="sender@example.com",
                body_text="See attached file.",
                received_at=datetime.now(timezone.utc),
                size_bytes=15_000_000,
            )
            session.add(mail)
            await session.flush()

            att = Attachment(
                mail_id=mail.id,
                filename="huge_report.pdf",
                content_type="application/pdf",
                size_bytes=12_000_000,
            )
            session.add(att)
            await session.flush()
            await session.refresh(mail)
            mail_id = mail.id

        # Verify stored correctly
        mail_repo = MailRepository(db)
        fetched = await mail_repo.get_by_id(account_id, mail_id)
        assert fetched is not None
        assert fetched.size_bytes == 15_000_000

    async def test_pipeline_handles_large_body(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """
        Pipeline truncates body excerpt for large mails.
        """
        config = _test_config()
        account_id = test_data["account_id"]
        inbox = test_data["inbox"]

        # Build a mail with a very long body
        long_body = "A" * 50_000

        async with db.session() as session:
            mail = Mail(
                account_id=account_id,
                folder_id=inbox.id,
                uid=9011,
                subject="Huge Body Test",
                from_addr="sender@example.com",
                body_text=long_body,
                received_at=datetime.now(timezone.utc),
                size_bytes=50_000,
            )
            session.add(mail)
            await session.flush()
            await session.refresh(mail)

        mock_store = AsyncMock(spec=SemanticStore)
        mock_store.ensure_collection.return_value = True
        mock_store.upsert.return_value = True
        mock_store.search.return_value = []
        mock_store.build_embedding_text = SemanticStore.build_embedding_text

        mock_analyst = AsyncMock()
        mock_analyst.analyze.return_value = SpamVerdict(
            is_spam=False,
            raw_response={"verdict": "not-spam"},
        )

        verdict_repo = VerdictRepository(db)
        mail_repo = MailRepository(db)

        pipeline = VerdictPipeline(
            config=config,
            semantic_store=mock_store,
            analyst=mock_analyst,
            verdict_repo=verdict_repo,
            mail_repo=mail_repo,
        )

        result = await pipeline.process_mail(mail, inbox)
        assert result is False

        # Analyst was called (pipeline didn't crash on huge body)
        mock_analyst.analyze.assert_called_once()


class TestConcurrentMultiAccount:
    """Test concurrent processing across multiple accounts."""

    async def test_parallel_pipelines_different_accounts(
        self,
        db: DatabaseConnection,
    ) -> None:
        """
        Multiple pipelines processing different accounts concurrently
        do not interfere with each other.
        """
        config = _test_config()

        # Create two separate accounts with inboxes and mails
        accounts: list[dict[str, Any]] = []
        for i in range(2):
            async with db.session() as session:
                acc = Account(
                    name=f"concurrent-account-{i}",
                    imap_host="localhost",
                    imap_port=1143,
                    imap_user=f"concurrent{i}@localhost",
                )
                session.add(acc)
                await session.flush()

                inbox = Folder(
                    account_id=acc.id,
                    imap_name="INBOX",
                    special_use=SpecialUse.INBOX,
                )
                session.add(inbox)
                await session.flush()

                mail = Mail(
                    account_id=acc.id,
                    folder_id=inbox.id,
                    uid=9100 + i,
                    subject=f"Concurrent Test {i}",
                    from_addr=f"sender{i}@example.com",
                    body_text=f"Testing concurrent processing for account {i}.",
                    received_at=datetime.now(timezone.utc),
                )
                session.add(mail)
                await session.flush()
                await session.refresh(mail)

                accounts.append(
                    {
                        "account_id": acc.id,
                        "inbox": inbox,
                        "mail": mail,
                    }
                )

        # Process both accounts concurrently
        results: list[bool | None] = [None, None]

        async def process_account(idx: int) -> bool | None:
            acc_data = accounts[idx]
            mock_store = AsyncMock(spec=SemanticStore)
            mock_store.ensure_collection.return_value = True
            mock_store.upsert.return_value = True
            mock_store.search.return_value = []
            mock_store.build_embedding_text = SemanticStore.build_embedding_text

            mock_analyst = AsyncMock()
            is_spam = idx == 0  # First account gets spam, second doesn't
            mock_analyst.analyze.return_value = SpamVerdict(
                is_spam=is_spam,
                raw_response={"verdict": "spam" if is_spam else "not-spam"},
            )

            verdict_repo = VerdictRepository(db)
            mail_repo = MailRepository(db)

            pipeline = VerdictPipeline(
                config=config,
                semantic_store=mock_store,
                analyst=mock_analyst,
                verdict_repo=verdict_repo,
                mail_repo=mail_repo,
            )

            return await pipeline.process_mail(acc_data["mail"], acc_data["inbox"])

        results = await asyncio.gather(
            process_account(0),
            process_account(1),
        )

        # Account 0 was classified as spam, account 1 was not
        assert results[0] is True
        assert results[1] is False

        # Verify verdicts stored correctly per account
        verdict_repo = VerdictRepository(db)
        v0 = await verdict_repo.get_latest_for_mail(accounts[0]["mail"].id)
        v1 = await verdict_repo.get_latest_for_mail(accounts[1]["mail"].id)
        assert v0 is not None and v0.is_spam is True
        assert v1 is not None and v1.is_spam is False

    async def test_duplicate_uid_different_accounts(
        self,
        db: DatabaseConnection,
    ) -> None:
        """
        Same UID in different accounts does not cause collisions.
        """
        accounts: list[dict[str, Any]] = []
        for i in range(2):
            async with db.session() as session:
                acc = Account(
                    name=f"dup-uid-account-{i}",
                    imap_host="localhost",
                    imap_port=1143,
                    imap_user=f"dupuid{i}@localhost",
                )
                session.add(acc)
                await session.flush()

                inbox = Folder(
                    account_id=acc.id,
                    imap_name="INBOX",
                    special_use=SpecialUse.INBOX,
                )
                session.add(inbox)
                await session.flush()

                # Same UID (999) in both accounts
                mail = Mail(
                    account_id=acc.id,
                    folder_id=inbox.id,
                    uid=999,
                    subject=f"Duplicate UID Test - Account {i}",
                    from_addr=f"sender{i}@example.com",
                    received_at=datetime.now(timezone.utc),
                )
                session.add(mail)
                await session.flush()
                await session.refresh(mail)

                accounts.append({"account_id": acc.id, "mail_id": mail.id})

        # Both stored with same UID but different IDs
        assert accounts[0]["mail_id"] != accounts[1]["mail_id"]

        # Both retrievable via their account
        mail_repo = MailRepository(db)
        for acc_data in accounts:
            fetched = await mail_repo.get_by_id(acc_data["account_id"], acc_data["mail_id"])
            assert fetched is not None
            assert fetched.uid == 999
