"""Tests for SpamEventProcessor's to/from-junk move detection (mocked deps)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.postimap.listener import PostimapEvent
from mail_verdict.spam.processor import SpamEventProcessor

_ACCOUNT_ID = uuid.uuid4()
_MESSAGE_ID = uuid.uuid4()
_JUNK_FOLDER_ID = uuid.uuid4()
_INBOX_FOLDER_ID = uuid.uuid4()
_ARCHIVE_FOLDER_ID = uuid.uuid4()


def _make_folder(folder_id: uuid.UUID, special_use: str | None) -> MagicMock:
    folder = MagicMock()
    folder.id = folder_id
    folder.special_use = special_use
    return folder


def _make_processor(
    folders_by_id: dict[uuid.UUID, MagicMock],
) -> tuple[SpamEventProcessor, MagicMock, MagicMock]:
    """A processor with mocked deps, plus direct handles to the feedback and folder mocks.

    Returning the mocks directly (rather than reading them back off the
    processor's private attributes) keeps assertions on plain MagicMocks --
    reading through the processor makes mypy resolve the attribute against
    the real SpamFeedbackHandler/FolderRepository types instead.
    """
    feedback = MagicMock(handle_moved_to_spam=AsyncMock(), handle_moved_from_spam=AsyncMock())
    folder_repo = MagicMock()
    folder_repo.get_by_id = AsyncMock(side_effect=lambda fid: folders_by_id.get(fid))

    processor = SpamEventProcessor(
        pipeline=MagicMock(),
        feedback=feedback,
        message_repo=MagicMock(),
        folder_repo=folder_repo,
        db=MagicMock(),
    )
    return processor, feedback, folder_repo


def _move_event(*, folder_id: uuid.UUID, old_folder_id: uuid.UUID | None) -> PostimapEvent:
    payload: dict[str, object] = {
        "v": 1, "type": "message", "op": "update", "id": str(_MESSAGE_ID),
        "account_id": str(_ACCOUNT_ID), "folder_id": str(folder_id),
        "changed": ["folder_id", "imap_uid"],
    }
    if old_folder_id is not None:
        payload["old_folder_id"] = str(old_folder_id)
    return PostimapEvent.from_payload(payload)


class TestMoveIntoJunk:
    """The existing 'moved into junk' path -- regression coverage while touching the sibling."""

    @pytest.mark.asyncio
    async def test_records_moved_to_spam(self) -> None:
        folders = {
            _JUNK_FOLDER_ID: _make_folder(_JUNK_FOLDER_ID, "junk"),
            _INBOX_FOLDER_ID: _make_folder(_INBOX_FOLDER_ID, None),
        }
        processor, feedback, _folder_repo = _make_processor(folders)
        event = _move_event(folder_id=_JUNK_FOLDER_ID, old_folder_id=_INBOX_FOLDER_ID)

        await processor.handle_message_event(event)

        feedback.handle_moved_to_spam.assert_awaited_once_with(
            mail_id=_MESSAGE_ID, account_id=_ACCOUNT_ID,
        )
        feedback.handle_moved_from_spam.assert_not_awaited()


class TestMoveOutOfJunk:
    """The new signal: a move whose source was junk and destination isn't."""

    @pytest.mark.asyncio
    async def test_records_moved_from_spam_when_source_was_junk(self) -> None:
        folders = {
            _JUNK_FOLDER_ID: _make_folder(_JUNK_FOLDER_ID, "junk"),
            _INBOX_FOLDER_ID: _make_folder(_INBOX_FOLDER_ID, None),
        }
        processor, feedback, _folder_repo = _make_processor(folders)
        event = _move_event(folder_id=_INBOX_FOLDER_ID, old_folder_id=_JUNK_FOLDER_ID)

        await processor.handle_message_event(event)

        feedback.handle_moved_from_spam.assert_awaited_once_with(
            mail_id=_MESSAGE_ID, account_id=_ACCOUNT_ID,
        )
        feedback.handle_moved_to_spam.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_signal_when_source_was_not_junk(self) -> None:
        """A move between two ordinary folders is not a spam correction."""
        folders = {
            _INBOX_FOLDER_ID: _make_folder(_INBOX_FOLDER_ID, None),
            _ARCHIVE_FOLDER_ID: _make_folder(_ARCHIVE_FOLDER_ID, "archive"),
        }
        processor, feedback, _folder_repo = _make_processor(folders)
        event = _move_event(folder_id=_ARCHIVE_FOLDER_ID, old_folder_id=_INBOX_FOLDER_ID)

        await processor.handle_message_event(event)

        feedback.handle_moved_from_spam.assert_not_awaited()
        feedback.handle_moved_to_spam.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_signal_when_old_folder_id_is_absent(self) -> None:
        """A move made outside this application carries no old_folder_id -- nothing fires.

        This is the documented gap (correlating the expunge+insert pair by
        message_id would catch it, but that's cross-folder dedup, not done
        here) -- this test pins today's behavior, not the future one.
        """
        folders = {_INBOX_FOLDER_ID: _make_folder(_INBOX_FOLDER_ID, None)}
        processor, feedback, _folder_repo = _make_processor(folders)
        event = _move_event(folder_id=_INBOX_FOLDER_ID, old_folder_id=None)

        await processor.handle_message_event(event)

        feedback.handle_moved_from_spam.assert_not_awaited()
        feedback.handle_moved_to_spam.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_crash_when_old_folder_no_longer_exists(self) -> None:
        """A vanished source folder is handled, not a KeyError/AttributeError."""
        folders = {_INBOX_FOLDER_ID: _make_folder(_INBOX_FOLDER_ID, None)}
        processor, feedback, _folder_repo = _make_processor(folders)
        event = _move_event(folder_id=_INBOX_FOLDER_ID, old_folder_id=uuid.uuid4())

        await processor.handle_message_event(event)

        feedback.handle_moved_from_spam.assert_not_awaited()


class TestNonMoveUpdatesAreIgnored:
    """The existing imap_uid-changed guard -- regression coverage."""

    @pytest.mark.asyncio
    async def test_update_without_imap_uid_change_never_looks_up_folders(self) -> None:
        """An update that never touched imap_uid cannot be a folder move."""
        processor, _feedback, folder_repo = _make_processor({})
        event = PostimapEvent.from_payload({
            "v": 1, "type": "message", "op": "update", "id": str(_MESSAGE_ID),
            "account_id": str(_ACCOUNT_ID), "folder_id": str(_INBOX_FOLDER_ID),
            "changed": ["is_seen"],
        })

        await processor.handle_message_event(event)

        folder_repo.get_by_id.assert_not_awaited()
