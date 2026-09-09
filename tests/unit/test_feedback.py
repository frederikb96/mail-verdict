"""Tests for SpamFeedbackHandler: user_feedback verdict recording and the
move that a human ruling applies alongside it."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mail_verdict.database.models import VerdictSource
from mail_verdict.spam.feedback import SpamFeedbackHandler


class _FakeSessionContext:
    """Just enough of an async context manager for `async with db.session()`."""

    async def __aenter__(self) -> MagicMock:
        return MagicMock()

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeDb:
    def session(self) -> _FakeSessionContext:
        return _FakeSessionContext()


def _make_handler() -> tuple[SpamFeedbackHandler, MagicMock]:
    """Create a feedback handler with a mock verdict repo and a session
    factory real enough for apply_human_ruling's own move."""
    verdict_repo = MagicMock()
    verdict_repo.create_verdict = AsyncMock()
    verdict_repo.get_current_verdict = AsyncMock(return_value=None)
    return SpamFeedbackHandler(db=_FakeDb(), verdict_repo=verdict_repo), verdict_repo


class _Verdict:
    """Minimal stand-in for a Verdict row."""

    def __init__(self, is_spam: bool) -> None:
        self.is_spam = is_spam


class TestApplyHumanRuling:
    """The one function every surface calls: reading-pane thumbs, a row's
    own Spam/Not-spam button, the review screen, and the MCP tool."""

    @pytest.mark.asyncio
    async def test_a_spam_ruling_always_moves_to_junk(self) -> None:
        """Confirming a spam verdict and correcting a clean one both land
        here -- the move does not depend on what the prior verdict said."""
        handler, verdict_repo = _make_handler()
        verdict_repo.get_current_verdict = AsyncMock(return_value=_Verdict(is_spam=False))
        junk_folder_id = uuid.uuid4()
        mail_id, account_id = uuid.uuid4(), uuid.uuid4()

        with (
            patch("mail_verdict.spam.feedback.FolderRepository") as folder_repo_cls,
            patch("mail_verdict.spam.feedback.move_message", new=AsyncMock()) as move_message,
        ):
            folder_repo_cls.return_value.resolve_special_folder = AsyncMock(
                return_value=junk_folder_id,
            )
            result = await handler.apply_human_ruling(mail_id, account_id, is_spam=True)

        assert result is True
        verdict_repo.create_verdict.assert_awaited_once()
        assert verdict_repo.create_verdict.call_args.kwargs["is_spam"] is True
        assert verdict_repo.create_verdict.call_args.kwargs["source"] == VerdictSource.USER_FEEDBACK
        folder_repo_cls.return_value.resolve_special_folder.assert_awaited_once_with(
            account_id, "junk",
        )
        move_message.assert_awaited_once()
        assert move_message.call_args.args[1:] == (mail_id, junk_folder_id)

    @pytest.mark.asyncio
    async def test_a_not_spam_ruling_reversing_spam_moves_to_inbox(self) -> None:
        """The rescue case: the prior verdict said spam, this ruling says
        otherwise, so it moves back -- always to the inbox, since nothing
        records where a message came from."""
        handler, verdict_repo = _make_handler()
        verdict_repo.get_current_verdict = AsyncMock(return_value=_Verdict(is_spam=True))
        inbox_folder_id = uuid.uuid4()
        mail_id, account_id = uuid.uuid4(), uuid.uuid4()

        with (
            patch("mail_verdict.spam.feedback.FolderRepository") as folder_repo_cls,
            patch("mail_verdict.spam.feedback.move_message", new=AsyncMock()) as move_message,
        ):
            folder_repo_cls.return_value.resolve_special_folder = AsyncMock(
                return_value=inbox_folder_id,
            )
            result = await handler.apply_human_ruling(mail_id, account_id, is_spam=False)

        assert result is True
        assert verdict_repo.create_verdict.call_args.kwargs["is_spam"] is False
        folder_repo_cls.return_value.resolve_special_folder.assert_awaited_once_with(
            account_id, "inbox",
        )
        move_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_confirming_an_already_clean_verdict_moves_nothing(self) -> None:
        """Clean plus up: the ruling is recorded, but there is nothing to
        rescue the message from, so it stays exactly where it is."""
        handler, verdict_repo = _make_handler()
        verdict_repo.get_current_verdict = AsyncMock(return_value=_Verdict(is_spam=False))
        mail_id, account_id = uuid.uuid4(), uuid.uuid4()

        with (
            patch("mail_verdict.spam.feedback.FolderRepository") as folder_repo_cls,
            patch("mail_verdict.spam.feedback.move_message", new=AsyncMock()) as move_message,
        ):
            result = await handler.apply_human_ruling(mail_id, account_id, is_spam=False)

        assert result is True
        assert verdict_repo.create_verdict.call_args.kwargs["is_spam"] is False
        folder_repo_cls.assert_not_called()
        move_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_not_spam_ruling_with_no_prior_verdict_moves_nothing(self) -> None:
        """Never classified at all -- there is still nothing to rescue
        the message from, the same as an already-clean verdict."""
        handler, verdict_repo = _make_handler()
        verdict_repo.get_current_verdict = AsyncMock(return_value=None)
        mail_id, account_id = uuid.uuid4(), uuid.uuid4()

        with patch("mail_verdict.spam.feedback.move_message", new=AsyncMock()) as move_message:
            result = await handler.apply_human_ruling(mail_id, account_id, is_spam=False)

        assert result is True
        move_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reads_the_prior_verdict_before_recording_the_new_one(self) -> None:
        """get_current_verdict has to run before create_verdict inserts
        the new row -- reading it after would always see the ruling just
        written, never what the verdict said before it, and a reversal
        would never be distinguishable from a confirmation."""
        handler, verdict_repo = _make_handler()
        calls: list[str] = []
        verdict_repo.get_current_verdict = AsyncMock(
            side_effect=lambda *a, **kw: calls.append("read") or _Verdict(is_spam=True),
        )
        verdict_repo.create_verdict = AsyncMock(side_effect=lambda **kw: calls.append("write"))

        with (
            patch("mail_verdict.spam.feedback.FolderRepository") as folder_repo_cls,
            patch("mail_verdict.spam.feedback.move_message", new=AsyncMock()),
        ):
            folder_repo_cls.return_value.resolve_special_folder = AsyncMock(
                return_value=uuid.uuid4(),
            )
            await handler.apply_human_ruling(uuid.uuid4(), uuid.uuid4(), is_spam=False)

        assert calls == ["read", "write"]

    @pytest.mark.asyncio
    async def test_no_folder_found_records_but_does_not_move(self) -> None:
        """An account with no junk folder at all -- the ruling is still
        recorded; the move is silently skipped rather than raised, since
        this function is called from surfaces with very different error
        handling of their own."""
        handler, verdict_repo = _make_handler()
        verdict_repo.get_current_verdict = AsyncMock(return_value=_Verdict(is_spam=False))

        with (
            patch("mail_verdict.spam.feedback.FolderRepository") as folder_repo_cls,
            patch("mail_verdict.spam.feedback.move_message", new=AsyncMock()) as move_message,
        ):
            folder_repo_cls.return_value.resolve_special_folder = AsyncMock(return_value=None)
            result = await handler.apply_human_ruling(uuid.uuid4(), uuid.uuid4(), is_spam=True)

        assert result is True
        move_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exception_recording_the_verdict_returns_false(self) -> None:
        """A repository exception recording the verdict is caught and
        returns False, not raised -- the same as every other write here."""
        handler, verdict_repo = _make_handler()
        verdict_repo.create_verdict = AsyncMock(side_effect=RuntimeError("DB down"))

        with (
            patch("mail_verdict.spam.feedback.FolderRepository") as folder_repo_cls,
            patch("mail_verdict.spam.feedback.move_message", new=AsyncMock()),
        ):
            folder_repo_cls.return_value.resolve_special_folder = AsyncMock(
                return_value=uuid.uuid4(),
            )
            result = await handler.apply_human_ruling(uuid.uuid4(), uuid.uuid4(), is_spam=True)

        assert result is False


class TestHandleFolderMoveToJunk:
    """Tests for the contradiction-gated folder-move listener path."""

    @pytest.mark.asyncio
    async def test_no_op_when_no_verdict_exists(self) -> None:
        """Nothing to contradict, so nothing is recorded -- a rule with a
        move-to-junk effect and no RecordVerdict of its own (e.g. "block
        this sender") produces exactly this same no-verdict move, and
        there is no way from here to tell that apart from a human drag,
        so neither is treated as a correction."""
        handler, verdict_repo = _make_handler()
        verdict_repo.get_current_verdict = AsyncMock(return_value=None)

        result = await handler.handle_folder_move_to_junk(uuid.uuid4(), uuid.uuid4())

        assert result is False
        verdict_repo.create_verdict.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_op_when_current_verdict_already_agrees(self) -> None:
        """The pipeline's own move-spam effect already wrote is_spam=True moments
        earlier in the same run -- this must not re-record it as a correction.
        The same gate is what stops apply_human_ruling's own move from
        being double-recorded once this listener sees it go by."""
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

    @pytest.mark.asyncio
    async def test_no_op_when_no_verdict_exists(self) -> None:
        """Nothing to contradict, so nothing is recorded -- symmetric with
        TestHandleFolderMoveToJunk.test_no_op_when_no_verdict_exists."""
        handler, verdict_repo = _make_handler()
        verdict_repo.get_current_verdict = AsyncMock(return_value=None)

        result = await handler.handle_folder_move_out_of_junk(
            uuid.uuid4(), uuid.uuid4(), destination_special_use=None,
        )

        assert result is False
        verdict_repo.create_verdict.assert_not_awaited()
