"""Tests for SpamFeedbackListener: routes a folder-move event, never arrival."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.postimap.listener import PostimapEvent
from mail_verdict.spam.processor import SpamFeedbackListener


def _make_listener() -> tuple[SpamFeedbackListener, MagicMock, MagicMock]:
    feedback = MagicMock()
    feedback.handle_folder_move_to_junk = AsyncMock(return_value=True)
    feedback.handle_folder_move_out_of_junk = AsyncMock(return_value=True)
    folder_repo = MagicMock()
    return SpamFeedbackListener(feedback=feedback, folder_repo=folder_repo), feedback, folder_repo


@pytest.mark.asyncio
async def test_ignores_insert_events() -> None:
    """Arrival is never this listener's concern -- that would be exactly the
    self-triggering loop the pipeline is triggered on arrival only to avoid."""
    listener, feedback, _ = _make_listener()
    event = PostimapEvent(
        v=1, type="message", op="insert", id=str(uuid.uuid4()), account_id=str(uuid.uuid4()),
    )

    await listener.handle_message_event(event)

    feedback.handle_folder_move_to_junk.assert_not_awaited()
    feedback.handle_folder_move_out_of_junk.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignores_updates_that_did_not_change_folder() -> None:
    """A seen/flagged update is not a move -- folder_id must be in `changed`."""
    listener, feedback, _ = _make_listener()
    event = PostimapEvent(
        v=1, type="message", op="update", id=str(uuid.uuid4()), account_id=str(uuid.uuid4()),
        folder_id=str(uuid.uuid4()), changed=("is_seen",),
    )

    await listener.handle_message_event(event)

    feedback.handle_folder_move_to_junk.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_into_junk_calls_the_contradiction_gated_handler() -> None:
    listener, feedback, folder_repo = _make_listener()
    folder_repo.get_effective_special_use = AsyncMock(return_value="junk")
    mail_id, account_id, folder_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    event = PostimapEvent(
        v=1, type="message", op="update", id=str(mail_id), account_id=str(account_id),
        folder_id=str(folder_id), changed=("folder_id",),
    )

    await listener.handle_message_event(event)

    feedback.handle_folder_move_to_junk.assert_awaited_once_with(
        mail_id=mail_id, account_id=account_id,
    )


@pytest.mark.asyncio
async def test_move_out_of_junk_passes_the_destination_special_use() -> None:
    listener, feedback, folder_repo = _make_listener()
    # First call resolves the destination, second the old folder.
    folder_repo.get_effective_special_use = AsyncMock(side_effect=[None, "junk"])
    mail_id, account_id = uuid.uuid4(), uuid.uuid4()
    old_folder_id, new_folder_id = uuid.uuid4(), uuid.uuid4()
    event = PostimapEvent(
        v=1, type="message", op="update", id=str(mail_id), account_id=str(account_id),
        folder_id=str(new_folder_id), old_folder_id=str(old_folder_id), changed=("folder_id",),
    )

    await listener.handle_message_event(event)

    feedback.handle_folder_move_out_of_junk.assert_awaited_once_with(
        mail_id=mail_id, account_id=account_id, destination_special_use=None,
    )


@pytest.mark.asyncio
async def test_move_between_two_non_junk_folders_is_a_no_op() -> None:
    """Neither side is junk -- nothing for the feedback handler to do."""
    listener, feedback, folder_repo = _make_listener()
    folder_repo.get_effective_special_use = AsyncMock(side_effect=[None, None])
    event = PostimapEvent(
        v=1, type="message", op="update", id=str(uuid.uuid4()), account_id=str(uuid.uuid4()),
        folder_id=str(uuid.uuid4()), old_folder_id=str(uuid.uuid4()), changed=("folder_id",),
    )

    await listener.handle_message_event(event)

    feedback.handle_folder_move_to_junk.assert_not_awaited()
    feedback.handle_folder_move_out_of_junk.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_with_no_old_folder_id_is_not_a_junk_exit() -> None:
    """No old_folder_id means the move did not happen inside this application
    (see the module docstring); there is nothing to correlate the exit against."""
    listener, feedback, folder_repo = _make_listener()
    folder_repo.get_effective_special_use = AsyncMock(return_value=None)
    event = PostimapEvent(
        v=1, type="message", op="update", id=str(uuid.uuid4()), account_id=str(uuid.uuid4()),
        folder_id=str(uuid.uuid4()), changed=("folder_id",),
    )

    await listener.handle_message_event(event)

    feedback.handle_folder_move_out_of_junk.assert_not_awaited()


def test_feedback_handler_is_reachable_without_a_private_attribute() -> None:
    """
    A caller outside this listener's own event path (the API's explicit
    spam/not_spam actions) reaches the same SpamFeedbackHandler this
    listener uses through a public property -- not by naming its private
    `_feedback` attribute.
    """
    listener, feedback, _ = _make_listener()
    assert listener.feedback is feedback
