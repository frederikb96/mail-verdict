"""
E2E: Error resilience.

Tests: IMAP disconnect/reconnect, Qdrant unavailable, LLM timeout.
Verifies system recovers gracefully without crashing.

Markers: @pytest.mark.e2e
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock

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
)
from mail_verdict.database.repository import MailRepository, VerdictRepository
from mail_verdict.semantic.store import SemanticStore
from mail_verdict.spam.analyst import SpamVerdict
from mail_verdict.spam.pipeline import VerdictPipeline
from mail_verdict.sync.connector import IMAPConnectionError

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
        qdrant=QdrantConfig(host="localhost", port=6334, collection_name="test_resilience"),
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
    """Seed base test data."""
    async with db.session() as session:
        acc = Account(
            name="e2e-resilience-account",
            imap_host="localhost",
            imap_port=1143,
            imap_user="resilience@localhost",
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


class TestQdrantUnavailable:
    """Test behavior when Qdrant is unavailable."""

    async def test_pipeline_handles_embed_failure(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """
        When SemanticStore.upsert fails, pipeline returns None gracefully.
        """
        config = _test_config()
        account_id = test_data["account_id"]
        inbox = test_data["inbox"]

        async with db.session() as session:
            mail = Mail(
                account_id=account_id,
                folder_id=inbox.id,
                uid=5001,
                subject="Test Qdrant Down",
                from_addr="test@example.com",
                body_text="Testing resilience when Qdrant is down.",
                received_at=datetime.now(timezone.utc),
            )
            session.add(mail)
            await session.flush()
            await session.refresh(mail)

        # SemanticStore that fails on upsert
        mock_store = AsyncMock(spec=SemanticStore)
        mock_store.ensure_collection.return_value = False
        mock_store.upsert.return_value = False
        mock_store.build_embedding_text = SemanticStore.build_embedding_text

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
        mock_analyst.analyze.assert_not_called()

    async def test_pipeline_handles_search_failure(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """
        When search fails, pipeline still works (with empty neighbors).
        """
        config = _test_config()
        account_id = test_data["account_id"]
        inbox = test_data["inbox"]

        async with db.session() as session:
            mail = Mail(
                account_id=account_id,
                folder_id=inbox.id,
                uid=5002,
                subject="Test Search Failure",
                from_addr="test@example.com",
                body_text="Testing when search returns empty due to failure.",
                received_at=datetime.now(timezone.utc),
            )
            session.add(mail)
            await session.flush()
            await session.refresh(mail)

        mock_store = AsyncMock(spec=SemanticStore)
        mock_store.ensure_collection.return_value = True
        mock_store.upsert.return_value = True
        mock_store.search.return_value = []  # Empty due to failure
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
        assert result is False  # Still classifies with empty neighbors


class TestLLMTimeout:
    """Test behavior when LLM times out or returns errors."""

    async def test_pipeline_handles_analyst_exception(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """
        When SpamAnalyst.analyze raises an exception, pipeline returns None.
        """
        config = _test_config()
        account_id = test_data["account_id"]
        inbox = test_data["inbox"]

        async with db.session() as session:
            mail = Mail(
                account_id=account_id,
                folder_id=inbox.id,
                uid=6001,
                subject="Test LLM Timeout",
                from_addr="test@example.com",
                body_text="Testing when LLM fails.",
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
        mock_analyst.analyze.side_effect = RuntimeError("LLM timeout after 3 attempts")

        verdict_repo = VerdictRepository(db)
        mail_repo = MailRepository(db)

        pipeline = VerdictPipeline(
            config=config,
            semantic_store=mock_store,
            analyst=mock_analyst,
            verdict_repo=verdict_repo,
            mail_repo=mail_repo,
        )

        # Pipeline should not raise, just return None
        result = await pipeline.process_mail(mail, inbox)
        assert result is None


class TestIMAPReconnection:
    """Test IMAP connector reconnection on failure."""

    async def test_connector_retries_on_failure(self) -> None:
        """
        Verify IMAPConnector.connect_with_retry retries and eventually fails.
        """
        from mail_verdict.config import AccountConfig

        bad_config = AccountConfig(
            name="bad-account",
            host="192.0.2.1",  # RFC 5737 TEST-NET (unreachable)
            port=993,
            username="user",
            password="pass",
        )
        retry = RetryConfig(
            max_retries=1,
            base_delay_seconds=0.05,
            max_delay_seconds=0.1,
            exponential_base=2.0,
        )

        from mail_verdict.sync.connector import IMAPConnector

        connector = IMAPConnector(bad_config, retry)

        with pytest.raises(IMAPConnectionError):
            await connector.connect_with_retry()

    async def test_feedback_handler_survives_qdrant_error(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """
        SpamFeedbackHandler should still create verdict even if Qdrant update fails.
        """
        from mail_verdict.spam.feedback import SpamFeedbackHandler

        account_id = test_data["account_id"]
        inbox = test_data["inbox"]

        async with db.session() as session:
            mail = Mail(
                account_id=account_id,
                folder_id=inbox.id,
                uid=7001,
                subject="Resilience Mail",
                received_at=datetime.now(timezone.utc),
            )
            session.add(mail)
            await session.flush()
            await session.refresh(mail)
            mail_id = mail.id

        # Qdrant that fails
        mock_qdrant = AsyncMock()
        mock_qdrant.set_payload.side_effect = Exception("Qdrant connection refused")

        mock_store = MagicMock(spec=SemanticStore)
        mock_store._qdrant = mock_qdrant
        mock_store._collection = "test"

        verdict_repo = VerdictRepository(db)
        handler = SpamFeedbackHandler(mock_store, verdict_repo)

        # Should still succeed (Qdrant failure is non-fatal)
        result = await handler.handle_moved_to_spam(mail_id, account_id)
        assert result is True

        # Verdict should still be stored
        verdict = await verdict_repo.get_latest_for_mail(mail_id)
        assert verdict is not None
        assert verdict.is_spam is True
