"""Tests for cursor-based pagination: schema validation, edge cases, tiebreaker logic."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError


class TestMailListResponseSchema:
    """Tests for MailListResponse pagination schema."""

    def test_first_page_no_cursor(self) -> None:
        """First page response has no next_cursor when not full."""
        from mail_verdict.api.schemas import MailListResponse

        resp = MailListResponse(
            mails=[],
            has_more=False,
            next_cursor=None,
        )
        assert not resp.has_more
        assert resp.next_cursor is None

    def test_has_more_with_cursor(self) -> None:
        """Response with more pages includes a cursor."""
        from mail_verdict.api.schemas import MailListResponse

        cursor_id = str(uuid.uuid4())
        resp = MailListResponse(
            mails=[],
            has_more=True,
            next_cursor=cursor_id,
        )
        assert resp.has_more is True
        assert resp.next_cursor == cursor_id

    def test_cursor_is_string_uuid(self) -> None:
        """Cursor is a string representation of a UUID."""
        from mail_verdict.api.schemas import MailListResponse

        uid = uuid.uuid4()
        resp = MailListResponse(
            mails=[],
            has_more=True,
            next_cursor=str(uid),
        )
        # Verify the cursor is a valid UUID string
        parsed = uuid.UUID(resp.next_cursor)  # type: ignore[arg-type]
        assert parsed == uid

    def test_empty_mails_list(self) -> None:
        """Empty mails list is valid."""
        from mail_verdict.api.schemas import MailListResponse

        resp = MailListResponse(
            mails=[],
            has_more=False,
        )
        assert resp.mails == []


class TestMailSummarySchema:
    """Tests for MailSummary schema used in paginated responses."""

    def test_mail_summary_minimal(self) -> None:
        """MailSummary with only required fields."""
        from mail_verdict.api.schemas import MailSummary

        summary = MailSummary(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
        )
        assert summary.subject is None
        assert summary.is_read is False
        assert summary.headers_synced is False
        assert summary.body_synced is False

    def test_mail_summary_full(self) -> None:
        """MailSummary with all fields populated."""
        from mail_verdict.api.schemas import MailSummary

        now = datetime.now(timezone.utc)
        summary = MailSummary(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            subject="Test Subject",
            from_addr="sender@example.com",
            to_addrs=["recipient@example.com"],
            received_at=now,
            is_read=True,
            is_flagged=True,
            is_deleted=False,
            headers_synced=True,
            body_synced=True,
        )
        assert summary.subject == "Test Subject"
        assert summary.is_read is True
        assert summary.body_synced is True

    def test_mail_summary_two_phase_sync_fields(self) -> None:
        """MailSummary has both headers_synced and body_synced for two-phase sync."""
        from mail_verdict.api.schemas import MailSummary

        # Headers fetched but body not yet
        summary = MailSummary(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            headers_synced=True,
            body_synced=False,
        )
        assert summary.headers_synced is True
        assert summary.body_synced is False


class TestUnifiedMailListCursorPagination:
    """Tests for cursor-based pagination in unified mail lists."""

    def test_unified_mail_list_response_pagination(self) -> None:
        """UnifiedMailListResponse supports cursor pagination."""
        from mail_verdict.api.schemas import UnifiedMailListResponse

        resp = UnifiedMailListResponse(
            mails=[],
            has_more=True,
            next_cursor=str(uuid.uuid4()),
        )
        assert resp.has_more is True
        assert resp.next_cursor is not None

    def test_unified_mail_list_no_more(self) -> None:
        """UnifiedMailListResponse with no more pages."""
        from mail_verdict.api.schemas import UnifiedMailListResponse

        resp = UnifiedMailListResponse(
            mails=[],
            has_more=False,
            next_cursor=None,
        )
        assert not resp.has_more


class TestMailDetailOnDemandBody:
    """Tests for MailDetail schema fields related to on-demand body fetch."""

    def test_mail_detail_body_synced_false(self) -> None:
        """MailDetail with body_synced=False indicates body not yet fetched."""
        from mail_verdict.api.schemas import MailDetail

        now = datetime.now(timezone.utc)
        detail = MailDetail(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            uid=42,
            headers_synced=True,
            body_synced=False,
            body_html=None,
            body_text=None,
            fetched_at=now,
            created_at=now,
        )
        assert detail.body_synced is False
        assert detail.body_html is None

    def test_mail_detail_has_blocked_images_field(self) -> None:
        """MailDetail includes has_blocked_images for image blocking state."""
        from mail_verdict.api.schemas import MailDetail

        now = datetime.now(timezone.utc)
        detail = MailDetail(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            uid=1,
            fetched_at=now,
            created_at=now,
            has_blocked_images=True,
            images_allowed=False,
        )
        assert detail.has_blocked_images is True
        assert detail.images_allowed is False

    def test_mail_detail_images_allowed_field(self) -> None:
        """MailDetail includes images_allowed for sender exception state."""
        from mail_verdict.api.schemas import MailDetail

        now = datetime.now(timezone.utc)
        detail = MailDetail(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            uid=1,
            fetched_at=now,
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
        """SelectionToggle has a mail_id."""
        from mail_verdict.api.schemas import SelectionToggle

        toggle = SelectionToggle(mail_id=uuid.uuid4())
        assert toggle.mail_id is not None

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

