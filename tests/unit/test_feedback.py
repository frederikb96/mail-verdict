"""Tests for SpamFeedbackHandler: user_feedback verdict recording."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.database.models import VerdictSource
from mail_verdict.spam.feedback import SpamFeedbackHandler


def _make_handler() -> tuple[SpamFeedbackHandler, MagicMock]:
    """Create a feedback handler with a mock verdict repo."""
    verdict_repo = MagicMock()
    verdict_repo.create_verdict = AsyncMock()
    return SpamFeedbackHandler(verdict_repo=verdict_repo), verdict_repo


class TestHandleMovedToSpam:
    """Tests for handle_moved_to_spam."""

    @pytest.mark.asyncio
    async def test_creates_user_feedback_verdict(self) -> None:
        """Records a USER_FEEDBACK verdict with is_spam=True."""
        handler, verdict_repo = _make_handler()
        mail_id = uuid.uuid4()
        account_id = uuid.uuid4()

        result = await handler.handle_moved_to_spam(mail_id, account_id)

        assert result is True
        verdict_repo.create_verdict.assert_awaited_once()
        call_kwargs = verdict_repo.create_verdict.call_args.kwargs
        assert call_kwargs["mail_id"] == mail_id
        assert call_kwargs["account_id"] == account_id
        assert call_kwargs["is_spam"] is True
        assert call_kwargs["source"] == VerdictSource.USER_FEEDBACK


class TestHandleMovedFromSpam:
    """Tests for handle_moved_from_spam."""

    @pytest.mark.asyncio
    async def test_creates_not_spam_verdict(self) -> None:
        """Records a USER_FEEDBACK verdict with is_spam=False."""
        handler, verdict_repo = _make_handler()

        result = await handler.handle_moved_from_spam(uuid.uuid4(), uuid.uuid4())

        assert result is True
        call_kwargs = verdict_repo.create_verdict.call_args.kwargs
        assert call_kwargs["is_spam"] is False


class TestErrorHandling:
    """Tests for error handling in feedback handler."""

    @pytest.mark.asyncio
    async def test_exception_returns_false(self) -> None:
        """A repository exception is caught and returns False, not raised."""
        handler, verdict_repo = _make_handler()
        verdict_repo.create_verdict = AsyncMock(side_effect=RuntimeError("DB down"))

        result = await handler.handle_moved_to_spam(uuid.uuid4(), uuid.uuid4())

        assert result is False
