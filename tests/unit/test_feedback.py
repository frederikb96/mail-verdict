"""
Unit tests for feedback loop: Qdrant tag updates on folder moves.

Row 154 (o=10.46): spam->inbox move updates tag, inbox->spam updates tag,
missing embedding graceful handling.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from mail_verdict.database.models import VerdictSource
from mail_verdict.spam.feedback import SpamFeedbackHandler

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_verdict_repo() -> AsyncMock:
    """Mock VerdictRepository."""
    repo = AsyncMock()
    repo.create_verdict = AsyncMock()
    return repo


@pytest.fixture
def handler(
    mock_semantic_store: AsyncMock,
    mock_verdict_repo: AsyncMock,
) -> SpamFeedbackHandler:
    """Create SpamFeedbackHandler with mocked dependencies."""
    return SpamFeedbackHandler(
        semantic_store=mock_semantic_store,
        verdict_repo=mock_verdict_repo,
    )


@pytest.fixture
def mail_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-000000000003")


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-000000000001")


class TestMovedToSpam:
    """Tests for handle_moved_to_spam."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_updates_qdrant_is_spam_true(
        self,
        handler: SpamFeedbackHandler,
        mock_semantic_store: AsyncMock,
        mock_verdict_repo: AsyncMock,
        mail_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> None:
        """Moving to spam sets is_spam=true in Qdrant."""
        result = await handler.handle_moved_to_spam(mail_id, account_id)
        assert result is True

        # Verify update_payload was called with is_spam: "true"
        mock_semantic_store.update_payload.assert_called_once()
        call_kwargs = mock_semantic_store.update_payload.call_args
        assert call_kwargs.kwargs["payload"]["is_spam"] == "true"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_creates_user_feedback_verdict(
        self,
        handler: SpamFeedbackHandler,
        mock_verdict_repo: AsyncMock,
        mail_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> None:
        """Moving to spam creates a user_feedback verdict."""
        await handler.handle_moved_to_spam(mail_id, account_id)

        mock_verdict_repo.create_verdict.assert_called_once()
        call_kwargs = mock_verdict_repo.create_verdict.call_args
        assert call_kwargs.kwargs["mail_id"] == mail_id
        assert call_kwargs.kwargs["is_spam"] is True
        assert call_kwargs.kwargs["source"] == VerdictSource.USER_FEEDBACK


class TestMovedFromSpam:
    """Tests for handle_moved_from_spam."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_updates_qdrant_is_spam_false(
        self,
        handler: SpamFeedbackHandler,
        mock_semantic_store: AsyncMock,
        mail_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> None:
        """Moving from spam sets is_spam=false in Qdrant."""
        result = await handler.handle_moved_from_spam(mail_id, account_id)
        assert result is True

        call_kwargs = mock_semantic_store.update_payload.call_args
        assert call_kwargs.kwargs["payload"]["is_spam"] == "false"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_creates_not_spam_verdict(
        self,
        handler: SpamFeedbackHandler,
        mock_verdict_repo: AsyncMock,
        mail_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> None:
        """Moving from spam creates a not-spam user_feedback verdict."""
        await handler.handle_moved_from_spam(mail_id, account_id)

        call_kwargs = mock_verdict_repo.create_verdict.call_args
        assert call_kwargs.kwargs["is_spam"] is False


class TestMissingEmbedding:
    """Tests for graceful handling when Qdrant data is missing."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_qdrant_failure_still_creates_verdict(
        self,
        handler: SpamFeedbackHandler,
        mock_semantic_store: AsyncMock,
        mock_verdict_repo: AsyncMock,
        mail_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> None:
        """Qdrant failure doesn't prevent verdict creation."""
        mock_semantic_store.update_payload.return_value = False

        result = await handler.handle_moved_to_spam(mail_id, account_id)
        assert result is True
        mock_verdict_repo.create_verdict.assert_called_once()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_full_failure_returns_false(
        self,
        handler: SpamFeedbackHandler,
        mock_semantic_store: AsyncMock,
        mock_verdict_repo: AsyncMock,
        mail_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> None:
        """If verdict creation also fails, returns False."""
        mock_semantic_store.update_payload.return_value = False
        mock_verdict_repo.create_verdict.side_effect = Exception("DB error")

        result = await handler.handle_moved_to_spam(mail_id, account_id)
        assert result is False
