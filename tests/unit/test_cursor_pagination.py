"""Tests for cursor-based pagination: schema validation, edge cases."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


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
            thread_id=uuid.uuid4(),
            mirrored_at=datetime.now(timezone.utc),
            has_attachments=False,
            verdict_is_spam=None,
        )
        assert summary.subject is None
        assert summary.is_seen is False
        assert summary.verdict_is_spam is None

    def test_message_summary_full(self) -> None:
        """MessageSummary with all fields populated."""
        from mail_verdict.api.schemas import MessageSummary

        now = datetime.now(timezone.utc)
        summary = MessageSummary(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            thread_id=uuid.uuid4(),
            subject="Test Subject",
            from_addr="sender@example.com",
            to_addrs=["recipient@example.com"],
            received_at=now,
            mirrored_at=now,
            is_seen=True,
            is_flagged=True,
            has_attachments=True,
            verdict_is_spam=True,
        )
        assert summary.subject == "Test Subject"
        assert summary.is_seen is True
        assert summary.has_attachments is True


class TestUnifiedMessageListCursorPagination:
    """A unified view's list answers in the same shape as an account's."""

    def test_unified_list_uses_the_account_list_response(self) -> None:
        from mail_verdict.api.schemas import MessageListResponse
        from mail_verdict.api.unified import unified_router

        route = next(r for r in unified_router.routes if r.path.endswith("/mails"))  # type: ignore[union-attr]
        assert route.response_model is MessageListResponse  # type: ignore[union-attr]


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
            thread_id=uuid.uuid4(),
            pending_sync=False,
            body_html=None,
            body_text=None,
            created_at=now,
        )
        assert detail.pending_sync is False
        assert detail.body_html is None

    def test_message_detail_pending_sync_field(self) -> None:
        """pending_sync reflects a folder move PostIMAP has not confirmed yet."""
        from mail_verdict.api.schemas import MessageDetail

        now = datetime.now(timezone.utc)
        detail = MessageDetail(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            thread_id=uuid.uuid4(),
            pending_sync=True,
            created_at=now,
        )
        assert detail.pending_sync is True

    def test_message_detail_has_blocked_images_field(self) -> None:
        """MessageDetail includes has_blocked_images for image blocking state."""
        from mail_verdict.api.schemas import MessageDetail

        now = datetime.now(timezone.utc)
        detail = MessageDetail(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            thread_id=uuid.uuid4(),
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
            thread_id=uuid.uuid4(),
            created_at=now,
            images_allowed=True,
        )
        assert detail.images_allowed is True

    def test_message_detail_embeds_verdict(self) -> None:
        """MessageDetail carries the current verdict inline, not as a separate fetch."""
        from mail_verdict.api.schemas import MessageDetail, VerdictResponse

        now = datetime.now(timezone.utc)
        mail_id = uuid.uuid4()
        detail = MessageDetail(
            id=mail_id,
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            thread_id=uuid.uuid4(),
            created_at=now,
            verdict=VerdictResponse(
                id=uuid.uuid4(), message_id=mail_id, is_spam=True,
                source="ai", created_at=now,
            ),
        )
        assert detail.verdict is not None
        assert detail.verdict.is_spam is True

        without_verdict = MessageDetail(
            id=mail_id, account_id=uuid.uuid4(), folder_id=uuid.uuid4(),
            thread_id=uuid.uuid4(), created_at=now,
        )
        assert without_verdict.verdict is None
