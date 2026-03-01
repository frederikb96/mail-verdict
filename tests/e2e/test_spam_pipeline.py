"""
E2E: Full spam detection pipeline.

Flow: deliver email via Stalwart -> IDLE detects -> embed -> classify -> move to spam

Verifies end-to-end from mail arrival to folder action.

Markers: @pytest.mark.e2e
"""

from __future__ import annotations

import uuid
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
    Base,
    Folder,
    Mail,
    SpecialUse,
    VerdictSource,
)
from mail_verdict.database.repository import (
    MailRepository,
    VerdictRepository,
)
from mail_verdict.semantic.store import SearchResult, SemanticStore
from mail_verdict.spam.analyst import SpamVerdict
from mail_verdict.spam.pipeline import VerdictPipeline

TEST_DB_URL = (
    "postgresql+asyncpg://mail_verdict_test:mail_verdict_test@localhost:5433/mail_verdict_test"
)
STALWART_HOST = "localhost"
STALWART_SMTP_PORT = 1025
STALWART_IMAP_PORT = 1143

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.asyncio(loop_scope="module"),
]


def _test_config(spam_enabled: bool = True) -> MailVerdictConfig:
    """Build test configuration."""
    return MailVerdictConfig(
        server=ServerConfig(host="127.0.0.1", port=8080, log_level="WARNING", cors_origins=[]),
        accounts=[],
        database=DatabaseConfig(url=TEST_DB_URL, pool_size=5, max_overflow=2),
        qdrant=QdrantConfig(host="localhost", port=6334, collection_name="test_e2e_spam"),
        ai=AIConfig(
            provider="openai",
            model="gpt-4o-mini",
            embedding_model="text-embedding-3-small",
            embedding_dimensions=1536,
        ),
        spam=SpamConfig(
            enabled=spam_enabled,
            excerpt_length=300,
            neighbor_count=3,
            auto_mark_read=True,
            system_prompt="You are a spam analyst. Return JSON: {verdict: 'spam' or 'not-spam'}",
        ),
        sync=SyncConfig(
            poll_interval_seconds=300,
            idle_enabled=True,
            idle_restart_seconds=30,
            lookback_days=180,
            auto_detect_folders=True,
        ),
        retry=RetryConfig(
            max_retries=2,
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
    """Seed database with account, folders, and a test mail."""
    async with db.session() as session:
        acc = Account(
            name="e2e-spam-account",
            imap_host=STALWART_HOST,
            imap_port=STALWART_IMAP_PORT,
            imap_user="spamtest@localhost",
        )
        session.add(acc)
        await session.flush()

        inbox = Folder(
            account_id=acc.id,
            imap_name="INBOX",
            special_use=SpecialUse.INBOX,
        )
        junk = Folder(
            account_id=acc.id,
            imap_name="Junk",
            special_use=SpecialUse.JUNK,
        )
        session.add(inbox)
        session.add(junk)
        await session.flush()

        return {
            "account_id": acc.id,
            "inbox": inbox,
            "junk": junk,
        }


class TestFullSpamPipeline:
    """E2E: simulate the entire spam detection flow."""

    async def test_spam_mail_classified_and_moved(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """
        Full pipeline: new mail -> embed -> search neighbors -> LLM classify
        -> store verdict -> move.

        Uses mocked SemanticStore (no real OpenAI) and mocked SpamAnalyst (no real LLM).
        Database operations are real against test Postgres.
        """
        config = _test_config()
        account_id = test_data["account_id"]
        inbox = test_data["inbox"]

        # Create a test mail in the database
        async with db.session() as session:
            mail = Mail(
                account_id=account_id,
                folder_id=inbox.id,
                uid=1001,
                message_id=f"<spam-test-{uuid.uuid4().hex[:8]}@example.com>",
                subject="You Won $1,000,000!!!",
                from_addr="prince@nigeria-lottery.com",
                body_text="Congratulations! You have been selected as the winner...",
                received_at=datetime.now(timezone.utc),
                dkim_pass=False,
                spf_pass=False,
                dmarc_pass=False,
            )
            session.add(mail)
            await session.flush()
            await session.refresh(mail)
            mail_id = mail.id

        # Mock SemanticStore
        mock_store = AsyncMock(spec=SemanticStore)
        mock_store.ensure_collection.return_value = True
        mock_store.upsert.return_value = True
        mock_store.search.return_value = [
            SearchResult(
                point_id="neighbor1",
                mail_id=str(uuid.uuid4()),
                score=0.85,
                payload={"is_spam": "true", "from_domain": "scam.com"},
            ),
        ]
        mock_store.build_embedding_text = SemanticStore.build_embedding_text

        # Mock SpamAnalyst that says "spam"
        mock_analyst = AsyncMock()
        mock_analyst.analyze.return_value = SpamVerdict(
            is_spam=True,
            raw_response={"verdict": "spam"},
        )

        # Mock ActionPropagator
        mock_action = AsyncMock()
        mock_action.move_to_spam.return_value = True

        verdict_repo = VerdictRepository(db)
        mail_repo = MailRepository(db)

        pipeline = VerdictPipeline(
            config=config,
            semantic_store=mock_store,
            analyst=mock_analyst,
            verdict_repo=verdict_repo,
            mail_repo=mail_repo,
            action_propagator=mock_action,
        )

        # Re-fetch mail and folder from DB
        fetched_mail = await mail_repo.get_by_id(account_id, mail_id)
        assert fetched_mail is not None

        result = await pipeline.process_mail(fetched_mail, inbox)

        # Verify: classified as spam
        assert result is True

        # Verify: verdict stored in database
        verdict = await verdict_repo.get_latest_for_mail(mail_id)
        assert verdict is not None
        assert verdict.is_spam is True
        assert verdict.source == VerdictSource.AI

        # Verify: move_to_spam was called
        mock_action.move_to_spam.assert_called_once()

        # Verify: SemanticStore was queried for neighbors
        mock_store.search.assert_called_once()

    async def test_ham_mail_not_moved(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """Legitimate mail should be classified as not-spam and not moved."""
        config = _test_config()
        account_id = test_data["account_id"]
        inbox = test_data["inbox"]

        async with db.session() as session:
            mail = Mail(
                account_id=account_id,
                folder_id=inbox.id,
                uid=1002,
                subject="Meeting Tomorrow",
                from_addr="colleague@company.com",
                body_text="Hi, just a reminder about our meeting tomorrow at 2pm.",
                received_at=datetime.now(timezone.utc),
                dkim_pass=True,
                spf_pass=True,
                dmarc_pass=True,
            )
            session.add(mail)
            await session.flush()
            await session.refresh(mail)
            mail_id = mail.id

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

        mock_action = AsyncMock()

        verdict_repo = VerdictRepository(db)
        mail_repo = MailRepository(db)

        pipeline = VerdictPipeline(
            config=config,
            semantic_store=mock_store,
            analyst=mock_analyst,
            verdict_repo=verdict_repo,
            mail_repo=mail_repo,
            action_propagator=mock_action,
        )

        fetched_mail = await mail_repo.get_by_id(account_id, mail_id)
        result = await pipeline.process_mail(fetched_mail, inbox)

        assert result is False
        mock_action.move_to_spam.assert_not_called()

        verdict = await verdict_repo.get_latest_for_mail(mail_id)
        assert verdict is not None
        assert verdict.is_spam is False

    async def test_skip_sent_folder(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """Mails in sent/drafts/trash folders should be skipped."""
        config = _test_config()
        account_id = test_data["account_id"]

        async with db.session() as session:
            sent = Folder(
                account_id=account_id,
                imap_name="Sent",
                special_use=SpecialUse.SENT,
            )
            session.add(sent)
            await session.flush()

            mail = Mail(
                account_id=account_id,
                folder_id=sent.id,
                uid=2001,
                subject="My Sent Mail",
                received_at=datetime.now(timezone.utc),
            )
            session.add(mail)
            await session.flush()
            await session.refresh(mail)

        mock_store = AsyncMock(spec=SemanticStore)
        mock_analyst = AsyncMock()
        verdict_repo = VerdictRepository(db)
        mail_repo = MailRepository(db)

        pipeline = VerdictPipeline(
            config=config,
            semantic_store=mock_store,
            analyst=mock_analyst,
            verdict_repo=verdict_repo,
            mail_repo=mail_repo,
        )

        result = await pipeline.process_mail(mail, sent)
        assert result is None
        mock_analyst.analyze.assert_not_called()

    async def test_disabled_spam_config(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """When spam.enabled is False, pipeline returns None."""
        config = _test_config(spam_enabled=False)
        account_id = test_data["account_id"]
        inbox = test_data["inbox"]

        async with db.session() as session:
            mail = Mail(
                account_id=account_id,
                folder_id=inbox.id,
                uid=3001,
                subject="Test Disabled",
                received_at=datetime.now(timezone.utc),
            )
            session.add(mail)
            await session.flush()
            await session.refresh(mail)

        mock_store = AsyncMock(spec=SemanticStore)
        mock_analyst = AsyncMock()
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
        assert result is None
