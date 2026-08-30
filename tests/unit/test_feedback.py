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


class _Verdict:
    """Minimal stand-in for a Verdict row."""

    def __init__(self, is_spam: bool) -> None:
        self.is_spam = is_spam


class TestHandleFolderMoveToJunk:
    """Tests for the contradiction-gated folder-move listener path."""

    @pytest.mark.asyncio
    async def test_records_a_correction_when_no_verdict_exists(self) -> None:
        """Nothing to contradict yet -- the move itself is the first label."""
        handler, verdict_repo = _make_handler()
        verdict_repo.get_current_verdict = AsyncMock(return_value=None)

        result = await handler.handle_folder_move_to_junk(uuid.uuid4(), uuid.uuid4())

        assert result is True
        assert verdict_repo.create_verdict.call_args.kwargs["is_spam"] is True

    @pytest.mark.asyncio
    async def test_no_op_when_current_verdict_already_agrees(self) -> None:
        """The pipeline's own move-spam effect already wrote is_spam=True moments
        earlier in the same run -- this must not re-record it as a correction."""
        handler, verdict_repo = _make_handler()
        verdict_repo.get_current_verdict = AsyncMock(return_value=_Verdict(is_spam=True))

        result = await handler.handle_folder_move_to_junk(uuid.uuid4(), uuid.uuid4())

        assert result is False
        verdict_repo.create_verdict.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_records_a_correction_when_current_verdict_disagrees(self) -> None:
        """The classifier said not-spam; the user moving it to junk is a correction."""
        handler, verdict_repo = _make_handler()
        verdict_repo.get_current_verdict = AsyncMock(return_value=_Verdict(is_spam=False))

        result = await handler.handle_folder_move_to_junk(uuid.uuid4(), uuid.uuid4())

        assert result is True


class TestHandleFolderMoveOutOfJunk:
    """Tests for leaving the junk folder, including the trash exception."""

    @pytest.mark.asyncio
    async def test_moving_to_trash_is_never_a_correction(self) -> None:
        """Deleting confirmed spam is the commonest action in a junk folder,
        not evidence the classifier was wrong."""
        handler, verdict_repo = _make_handler()
        verdict_repo.get_current_verdict = AsyncMock(return_value=_Verdict(is_spam=True))

        result = await handler.handle_folder_move_out_of_junk(
            uuid.uuid4(), uuid.uuid4(), destination_special_use="trash",
        )

        assert result is False
        verdict_repo.get_current_verdict.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_records_a_correction_when_verdict_said_spam(self) -> None:
        """Moved out to an ordinary folder while the verdict still says spam."""
        handler, verdict_repo = _make_handler()
        verdict_repo.get_current_verdict = AsyncMock(return_value=_Verdict(is_spam=True))

        result = await handler.handle_folder_move_out_of_junk(
            uuid.uuid4(), uuid.uuid4(), destination_special_use=None,
        )

        assert result is True
        assert verdict_repo.create_verdict.call_args.kwargs["is_spam"] is False

    @pytest.mark.asyncio
    async def test_no_op_when_current_verdict_already_agrees(self) -> None:
        """Verdict already says not-spam -- nothing to correct."""
        handler, verdict_repo = _make_handler()
        verdict_repo.get_current_verdict = AsyncMock(return_value=_Verdict(is_spam=False))

        result = await handler.handle_folder_move_out_of_junk(
            uuid.uuid4(), uuid.uuid4(), destination_special_use=None,
        )

        assert result is False
        verdict_repo.create_verdict.assert_not_awaited()
