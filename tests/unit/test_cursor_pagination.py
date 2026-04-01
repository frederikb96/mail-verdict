"""Tests for cursor-based pagination: schema validation, edge cases."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError


class TestMessageListResponseSchema:
    """Tests for MessageListResponse pagination schema."""

    def test_first_page_no_cursor(self) -> None:
        """First page response has no next_cursor when not full."""
        from mail_verdict.api.schemas import MessageListResponse

        resp = MessageListResponse(
            messages=[],
            has_more=False,
            next_cursor=None,
        )
        assert not resp.has_more
        assert resp.next_cursor is None

    def test_has_more_with_cursor(self) -> None:
        """Response with more pages includes a cursor."""
        from mail_verdict.api.schemas import MessageListResponse

        cursor_id = str(uuid.uuid4())
        resp = MessageListResponse(
            messages=[],
            has_more=True,
            next_cursor=cursor_id,
        )
        assert resp.has_more is True
        assert resp.next_cursor == cursor_id

    def test_cursor_is_string_uuid(self) -> None:
        """Cursor is a string representation of a UUID."""
        from mail_verdict.api.schemas import MessageListResponse

        uid = uuid.uuid4()
        resp = MessageListResponse(
            messages=[],
            has_more=True,
            next_cursor=str(uid),
        )
        # Verify the cursor is a valid UUID string
        parsed = uuid.UUID(resp.next_cursor)  # type: ignore[arg-type]
        assert parsed == uid

    def test_empty_messages_list(self) -> None:
        """Empty messages list is valid."""
        from mail_verdict.api.schemas import MessageListResponse

        resp = MessageListResponse(
            messages=[],
            has_more=False,
        )
        assert resp.messages == []


class TestMessageSummarySchema:
    """Tests for MessageSummary schema used in paginated responses."""

    def test_message_summary_minimal(self) -> None:
        """MessageSummary with only required fields."""
        from mail_verdict.api.schemas import MessageSummary

        summary = MessageSummary(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
        )
        assert summary.subject is None
        assert summary.is_seen is False

    def test_message_summary_full(self) -> None:
        """MessageSummary with all fields populated."""
        from mail_verdict.api.schemas import MessageSummary

        now = datetime.now(timezone.utc)
        summary = MessageSummary(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            subject="Test Subject",
            from_addr="sender@example.com",
            to_addrs=["recipient@example.com"],
            received_at=now,
            is_seen=True,
            is_flagged=True,
            is_deleted=False,
        )
        assert summary.subject == "Test Subject"
        assert summary.is_seen is True


class TestUnifiedMessageListCursorPagination:
    """Tests for cursor-based pagination in unified message lists."""

    def test_unified_message_list_response_pagination(self) -> None:
        """UnifiedMessageListResponse supports cursor pagination."""
        from mail_verdict.api.schemas import UnifiedMessageListResponse

        resp = UnifiedMessageListResponse(
            messages=[],
            has_more=True,
            next_cursor=str(uuid.uuid4()),
        )
        assert resp.has_more is True
        assert resp.next_cursor is not None

    def test_unified_message_list_no_more(self) -> None:
        """UnifiedMessageListResponse with no more pages."""
        from mail_verdict.api.schemas import UnifiedMessageListResponse

        resp = UnifiedMessageListResponse(
            messages=[],
            has_more=False,
            next_cursor=None,
        )
        assert not resp.has_more


class TestMessageDetailSchema:
    """Tests for MessageDetail schema fields."""

    def test_message_detail_basic(self) -> None:
        """MessageDetail with core fields."""
        from mail_verdict.api.schemas import MessageDetail

        now = datetime.now(timezone.utc)
        detail = MessageDetail(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            imap_uid=42,
            body_html=None,
            body_text=None,
            created_at=now,
        )
        assert detail.imap_uid == 42
        assert detail.body_html is None

    def test_message_detail_has_blocked_images_field(self) -> None:
        """MessageDetail includes has_blocked_images for image blocking state."""
        from mail_verdict.api.schemas import MessageDetail

        now = datetime.now(timezone.utc)
        detail = MessageDetail(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            imap_uid=1,
            created_at=now,
            has_blocked_images=True,
            images_allowed=False,
        )
        assert detail.has_blocked_images is True
        assert detail.images_allowed is False

    def test_message_detail_images_allowed_field(self) -> None:
        """MessageDetail includes images_allowed for sender exception state."""
        from mail_verdict.api.schemas import MessageDetail

        now = datetime.now(timezone.utc)
        detail = MessageDetail(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            imap_uid=1,
            created_at=now,
            images_allowed=True,
        )
        assert detail.images_allowed is True


class TestSelectionSchemas:
    """Tests for selection and bulk action schemas."""

    def test_selection_response_schema(self) -> None:
        """SelectionResponse has selected_ids and count."""
        from mail_verdict.api.schemas import SelectionResponse

        ids = [uuid.uuid4(), uuid.uuid4()]
        resp = SelectionResponse(selected_ids=ids, count=len(ids))
        assert resp.count == 2
        assert len(resp.selected_ids) == 2

    def test_selection_toggle_schema(self) -> None:
        """SelectionToggle has a message_id."""
        from mail_verdict.api.schemas import SelectionToggle

        toggle = SelectionToggle(message_id=uuid.uuid4())
        assert toggle.message_id is not None

    def test_selection_range_schema(self) -> None:
        """SelectionRange has from_id, to_id, and folder_id."""
        from mail_verdict.api.schemas import SelectionRange

        sr = SelectionRange(
            from_id=uuid.uuid4(),
            to_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
        )
        assert sr.from_id is not None
        assert sr.folder_id is not None

    def test_selection_all_schema(self) -> None:
        """SelectionAll has a folder_id."""
        from mail_verdict.api.schemas import SelectionAll

        sa = SelectionAll(folder_id=uuid.uuid4())
        assert sa.folder_id is not None

    def test_bulk_action_request_valid_actions(self) -> None:
        """BulkActionRequest accepts all valid action types."""
        from mail_verdict.api.schemas import BulkActionRequest

        valid_actions = [
            "move", "archive", "spam", "star", "unstar",
            "mark_read", "mark_unread", "delete",
        ]
        for action in valid_actions:
            req = BulkActionRequest(action=action)  # type: ignore[arg-type]
            assert req.action == action

    def test_bulk_action_request_invalid_action(self) -> None:
        """BulkActionRequest rejects invalid action types."""
        from mail_verdict.api.schemas import BulkActionRequest

        with pytest.raises(ValidationError):
            BulkActionRequest(action="invalid_action")  # type: ignore[arg-type]

    def test_bulk_action_request_with_target(self) -> None:
        """BulkActionRequest accepts optional target_folder_id."""
        from mail_verdict.api.schemas import BulkActionRequest

        target = uuid.uuid4()
        req = BulkActionRequest(action="move", target_folder_id=target)
        assert req.target_folder_id == target

    def test_bulk_action_response_schema(self) -> None:
        """BulkActionResponse has success, action, affected_count, errors."""
        from mail_verdict.api.schemas import BulkActionResponse

        resp = BulkActionResponse(
            success=True,
            action="mark_read",
            affected_count=5,
            errors=[],
        )
        assert resp.success is True
        assert resp.affected_count == 5
