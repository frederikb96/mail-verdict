"""Tests for SpamFeedbackHandler: Qdrant tag updates on folder moves."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.spam.feedback import SpamFeedbackHandler


def _make_handler(
    qdrant_ok: bool = True,
) -> tuple[SpamFeedbackHandler, dict[str, MagicMock]]:
    """Create a feedback handler with mock dependencies."""
    store = MagicMock()
    store.update_payload = AsyncMock(return_value=qdrant_ok)

    verdict_repo = MagicMock()
    verdict_repo.create_verdict = AsyncMock()

    handler = SpamFeedbackHandler(
        semantic_store=store,
        verdict_repo=verdict_repo,
    )
    return handler, {"store": store, "verdict_repo": verdict_repo}


class TestHandleMovedToSpam:
    """Tests for handle_moved_to_spam."""

    @pytest.mark.asyncio
    async def test_updates_qdrant_and_creates_verdict(self) -> None:
        """Records is_spam=true in Qdrant and creates USER_FEEDBACK verdict."""
        handler, mocks = _make_handler()
        mail_id = uuid.uuid4()
        account_id = uuid.uuid4()

        result = await handler.handle_moved_to_spam(mail_id, account_id)
        assert result is True
        mocks["store"].update_payload.assert_awaited_once()
        call_args = mocks["store"].update_payload.call_args
        assert call_args.kwargs["payload"]["is_spam"] == "true"
        mocks["verdict_repo"].create_verdict.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_qdrant_failure_still_creates_verdict(self) -> None:
        """Even if Qdrant update fails, verdict is still created."""
        handler, mocks = _make_handler(qdrant_ok=False)
        result = await handler.handle_moved_to_spam(uuid.uuid4(), uuid.uuid4())
        assert result is True
        mocks["verdict_repo"].create_verdict.assert_awaited_once()


class TestHandleMovedFromSpam:
    """Tests for handle_moved_from_spam."""

    @pytest.mark.asyncio
    async def test_updates_to_not_spam(self) -> None:
        """Records is_spam=false in Qdrant."""
        handler, mocks = _make_handler()
        result = await handler.handle_moved_from_spam(uuid.uuid4(), uuid.uuid4())
        assert result is True
        call_args = mocks["store"].update_payload.call_args
        assert call_args.kwargs["payload"]["is_spam"] == "false"


class TestErrorHandling:
    """Tests for error handling in feedback handler."""

    @pytest.mark.asyncio
    async def test_exception_returns_false(self) -> None:
        """Exception in verdict creation returns False."""
        handler, mocks = _make_handler()
        mocks["verdict_repo"].create_verdict = AsyncMock(
            side_effect=RuntimeError("DB down")
        )
        result = await handler.handle_moved_to_spam(uuid.uuid4(), uuid.uuid4())
        assert result is False
