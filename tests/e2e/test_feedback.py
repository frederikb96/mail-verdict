"""
E2E: Feedback loop.

Flow: user moves mail from spam->inbox -> Qdrant tag updated ->
      next similar mail classified correctly.

Verifies the feedback handler updates Qdrant and creates correction verdicts.

Markers: @pytest.mark.e2e
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock

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
from mail_verdict.database.repository import (
    MailRepository,
    VerdictRepository,
)
from mail_verdict.semantic.store import SearchResult, SemanticStore
from mail_verdict.spam.analyst import SpamVerdict
from mail_verdict.spam.feedback import SpamFeedbackHandler

TEST_DB_URL = (
    "postgresql+asyncpg://mail_verdict_test:mail_verdict_test@localhost:5433/mail_verdict_test"
)

pytestmark = [
    pytest.mark.e2e,
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
async def test_data(db: DatabaseConnection) -> dict[str, Any]:
    """Seed database with account, folders, and test mails."""
    async with db.session() as session:
        acc = Account(
            name="e2e-feedback-account",
            imap_host="localhost",
            imap_port=1143,
            imap_user="feedback@localhost",
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

        # A mail originally classified as spam (false positive)
        fp_mail = Mail(
            account_id=acc.id,
            folder_id=junk.id,
            uid=100,
            message_id=f"<fp-{uuid.uuid4().hex[:8]}@example.com>",
            subject="Your Order Confirmation #12345",
            from_addr="orders@legitimatestore.com",
            body_text="Thank you for your order. Your order #12345 has been confirmed.",
            received_at=datetime.now(timezone.utc),
            dkim_pass=True,
            spf_pass=True,
            dmarc_pass=True,
        )
        session.add(fp_mail)
        await session.flush()

        # Record the original AI verdict (spam)
        ai_verdict = Verdict(
            mail_id=fp_mail.id,
            is_spam=True,
            source=VerdictSource.AI,
            model_used="gpt-4o-mini",
        )
        session.add(ai_verdict)
        await session.flush()

        # A similar mail that should benefit from the correction
        similar_mail = Mail(
            account_id=acc.id,
            folder_id=inbox.id,
            uid=101,
            message_id=f"<similar-{uuid.uuid4().hex[:8]}@example.com>",
            subject="Your Order Confirmation #67890",
            from_addr="orders@legitimatestore.com",
            body_text="Thank you for your order. Your order #67890 has been confirmed.",
            received_at=datetime.now(timezone.utc),
            dkim_pass=True,
            spf_pass=True,
            dmarc_pass=True,
        )
        session.add(similar_mail)
        await session.flush()

        return {
            "account_id": acc.id,
            "inbox": inbox,
            "junk": junk,
            "fp_mail_id": fp_mail.id,
            "similar_mail_id": similar_mail.id,
        }


class TestFeedbackLoop:
    """E2E: user correction updates Qdrant and improves future classification."""

    async def test_user_corrects_false_positive(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """
        User moves mail from spam to inbox.

        Expected:
        - Qdrant is_spam tag updated to "false"
        - Correction verdict (USER_FEEDBACK, is_spam=False) stored
        """
        mock_qdrant = AsyncMock()
        mock_qdrant.set_payload = AsyncMock()

        mock_store = MagicMock(spec=SemanticStore)
        mock_store._qdrant = mock_qdrant
        mock_store._collection = "test_feedback"

        verdict_repo = VerdictRepository(db)
        handler = SpamFeedbackHandler(mock_store, verdict_repo)

        # User moves the false positive from spam to inbox
        result = await handler.handle_moved_from_spam(
            mail_id=test_data["fp_mail_id"],
            account_id=test_data["account_id"],
        )

        assert result is True

        # Verify: Qdrant tag was updated
        mock_qdrant.set_payload.assert_called_once()
        call_kwargs = mock_qdrant.set_payload.call_args
        assert call_kwargs.kwargs["payload"]["is_spam"] == "false"

        # Verify: correction verdict stored
        latest = await verdict_repo.get_latest_for_mail(test_data["fp_mail_id"])
        assert latest is not None
        assert latest.source == VerdictSource.USER_FEEDBACK
        assert latest.is_spam is False

    async def test_user_marks_as_spam(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """
        User moves a mail to spam folder.

        Expected:
        - Qdrant is_spam tag updated to "true"
        - Correction verdict (USER_FEEDBACK, is_spam=True) stored
        """
        mock_qdrant = AsyncMock()
        mock_store = MagicMock(spec=SemanticStore)
        mock_store._qdrant = mock_qdrant
        mock_store._collection = "test_feedback"

        verdict_repo = VerdictRepository(db)
        handler = SpamFeedbackHandler(mock_store, verdict_repo)

        result = await handler.handle_moved_to_spam(
            mail_id=test_data["similar_mail_id"],
            account_id=test_data["account_id"],
        )

        assert result is True

        # Verify Qdrant update
        mock_qdrant.set_payload.assert_called_once()
        call_kwargs = mock_qdrant.set_payload.call_args
        assert call_kwargs.kwargs["payload"]["is_spam"] == "true"

        # Verify verdict
        latest = await verdict_repo.get_latest_for_mail(test_data["similar_mail_id"])
        assert latest is not None
        assert latest.source == VerdictSource.USER_FEEDBACK
        assert latest.is_spam is True

    async def test_correction_improves_future_classification(
        self, db: DatabaseConnection, test_data: dict[str, Any]
    ) -> None:
        """
        After user correction, the next similar mail should be classified differently.

        Simulates: correction updates Qdrant tag -> future search returns corrected neighbor
        -> analyst sees corrected data.
        """
        from mail_verdict.config import (
            AIConfig,
            MailVerdictConfig,
            MCPConfig,
            QdrantConfig,
            RetryConfig,
            ServerConfig,
            SpamConfig,
            SyncConfig,
        )
        from mail_verdict.spam.pipeline import VerdictPipeline

        config = MailVerdictConfig(
            server=ServerConfig(host="127.0.0.1", port=8080, log_level="WARNING", cors_origins=[]),
            accounts=[],
            database=DatabaseConfig(url=TEST_DB_URL, pool_size=5, max_overflow=2),
            qdrant=QdrantConfig(host="localhost", port=6334, collection_name="test_correction"),
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

        account_id = test_data["account_id"]
        inbox = test_data["inbox"]

        # Create a new mail similar to the corrected one
        async with db.session() as session:
            new_mail = Mail(
                account_id=account_id,
                folder_id=inbox.id,
                uid=200,
                subject="Your Order Confirmation #99999",
                from_addr="orders@legitimatestore.com",
                body_text="Thank you for your order. Your order #99999 has been confirmed.",
                received_at=datetime.now(timezone.utc),
                dkim_pass=True,
                spf_pass=True,
                dmarc_pass=True,
            )
            session.add(new_mail)
            await session.flush()
            await session.refresh(new_mail)
            new_mail_id = new_mail.id

        # Mock store returns the corrected neighbor (now tagged as not-spam)
        mock_store = AsyncMock(spec=SemanticStore)
        mock_store.ensure_collection.return_value = True
        mock_store.upsert.return_value = True
        mock_store.search.return_value = [
            SearchResult(
                point_id=str(test_data["fp_mail_id"]),
                mail_id=str(test_data["fp_mail_id"]),
                score=0.95,
                payload={"is_spam": "false", "from_domain": "legitimatestore.com"},
            ),
        ]
        mock_store.build_embedding_text = SemanticStore.build_embedding_text

        # Analyst should now say not-spam because neighbor is corrected
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

        fetched = await mail_repo.get_by_id(account_id, new_mail_id)
        result = await pipeline.process_mail(fetched, inbox)

        assert result is False  # not spam thanks to corrected neighbor

        verdict = await verdict_repo.get_latest_for_mail(new_mail_id)
        assert verdict is not None
        assert verdict.is_spam is False
