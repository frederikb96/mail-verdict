"""Tests for SSE event conversion, formatting, and push helpers."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from mail_verdict.api.events import (
    _format_sse,
    _sync_event_to_sse,
)
from mail_verdict.sync.events import (
    FlagsChanged,
    MailDeleted,
    MailMoved,
    MailReceived,
    MailSpamDetected,
    MailTrashed,
)


class TestFormatSSE:
    """Tests for _format_sse() SSE message formatter."""

    def test_basic_format(self) -> None:
        """Formats an SSE message with id, event, and data."""
        result = _format_sse(1, "sync.state", {"phase": "idle"})
        assert "id: 1\n" in result
        assert "event: sync.state\n" in result
        assert '"phase": "idle"' in result
        assert result.endswith("\n\n")

    def test_data_is_json(self) -> None:
        """Data payload is valid JSON."""
        result = _format_sse(42, "mail.new", {"uid": 100, "folder_id": "abc"})
        lines = result.strip().split("\n")
        data_line = [line for line in lines if line.startswith("data: ")][0]
        parsed = json.loads(data_line[6:])
        assert parsed["uid"] == 100

    def test_multiline_format(self) -> None:
        """SSE format has proper line structure."""
        result = _format_sse(1, "test", {"key": "value"})
        parts = result.split("\n")
        assert parts[0].startswith("id: ")
        assert parts[1].startswith("event: ")
        assert parts[2].startswith("data: ")


class TestSyncEventToSSE:
    """Tests for _sync_event_to_sse() event conversion."""

    def _make_event_kwargs(self) -> dict:
        """Common event constructor kwargs."""
        return {
            "account_id": uuid.uuid4(),
            "folder_id": uuid.uuid4(),
            "uid": 42,
        }

    def test_mail_received_maps_to_mail_new(self) -> None:
        """MailReceived maps to mail.new event type."""
        event = MailReceived(**self._make_event_kwargs())
        event_type, data = _sync_event_to_sse(event)
        assert event_type == "mail.new"
        assert "uid" in data

    def test_mail_deleted_maps_to_mail_deleted(self) -> None:
        """MailDeleted maps to mail.deleted event type."""
        event = MailDeleted(**self._make_event_kwargs())
        event_type, data = _sync_event_to_sse(event)
        assert event_type == "mail.deleted"

    def test_mail_moved_maps_to_mail_updated(self) -> None:
        """MailMoved maps to mail.updated event type."""
        kwargs = self._make_event_kwargs()
        kwargs["from_folder_id"] = uuid.uuid4()
        event = MailMoved(**kwargs)
        event_type, data = _sync_event_to_sse(event)
        assert event_type == "mail.updated"

    def test_mail_trashed_maps_to_mail_updated(self) -> None:
        """MailTrashed maps to mail.updated event type."""
        event = MailTrashed(**self._make_event_kwargs())
        event_type, data = _sync_event_to_sse(event)
        assert event_type == "mail.updated"

    def test_mail_spam_detected_maps_to_mail_updated(self) -> None:
        """MailSpamDetected maps to mail.updated event type."""
        event = MailSpamDetected(**self._make_event_kwargs())
        event_type, data = _sync_event_to_sse(event)
        assert event_type == "mail.updated"

    def test_flags_changed_maps_to_mail_updated(self) -> None:
        """FlagsChanged maps to mail.updated with read/flagged state."""
        event = FlagsChanged(
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
            uid=10,
            is_read=True,
            is_flagged=False,
        )
        event_type, data = _sync_event_to_sse(event)
        assert event_type == "mail.updated"
        assert data["is_read"] is True
        assert data["is_flagged"] is False

    def test_data_includes_account_and_folder(self) -> None:
        """SSE data includes account_id and folder_id."""
        kwargs = self._make_event_kwargs()
        event = MailReceived(**kwargs)
        _, data = _sync_event_to_sse(event)
        assert "account_id" in data
        assert "folder_id" in data

    def test_data_includes_timestamp(self) -> None:
        """SSE data includes ISO timestamp."""
        event = MailReceived(**self._make_event_kwargs())
        _, data = _sync_event_to_sse(event)
        assert "timestamp" in data
        # Verify it's a parseable ISO timestamp
        datetime.fromisoformat(data["timestamp"])


class TestSelectionRouterRegistration:
    """Tests for selection router registration."""

    def test_selection_router_in_all_routers(self) -> None:
        """Selection router is registered."""
        from mail_verdict.api.routes import all_routers

        prefixes = [r.prefix for r in all_routers]
        assert any("selection" in p for p in prefixes)

    def test_selection_router_endpoints(self) -> None:
        """Selection router has expected endpoints."""
        from mail_verdict.api.selection import router

        routes = [r.path for r in router.routes]  # type: ignore[union-attr]
        assert any("toggle" in r for r in routes)
        assert any("range" in r for r in routes)
        assert any("all" in r for r in routes)
        assert any("clear" in r for r in routes)
        assert any("action" in r for r in routes)
