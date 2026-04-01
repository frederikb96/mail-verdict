"""Tests for unified view: schemas, model fields, and API schema validation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


class TestUnifiedViewModels:
    """Tests for unified view model columns (PostIMAP + Prefs split)."""

    def test_folder_prefs_has_unified_name(self) -> None:
        """FolderPrefs model includes unified_name column."""
        from mail_verdict.database.models import FolderPrefs

        assert hasattr(FolderPrefs, "unified_name")

    def test_account_prefs_has_emoji(self) -> None:
        """AccountPrefs model includes emoji column."""
        from mail_verdict.database.models import AccountPrefs

        assert hasattr(AccountPrefs, "emoji")


class TestUnifiedViewSchemas:
    """Tests for unified view Pydantic schemas."""

    def test_unified_folder_source_schema(self) -> None:
        """UnifiedFolderSource has required fields."""
        from mail_verdict.api.schemas import UnifiedFolderSource

        src = UnifiedFolderSource(
            account_id=uuid.uuid4(),
            account_name="Test",
            account_emoji="📧",
            folder_id=uuid.uuid4(),
            imap_name="INBOX",
        )
        assert src.account_emoji == "📧"
        assert src.imap_name == "INBOX"

    def test_unified_folder_response_schema(self) -> None:
        """UnifiedFolderResponse aggregates sources and counts."""
        from mail_verdict.api.schemas import UnifiedFolderResponse, UnifiedFolderSource

        resp = UnifiedFolderResponse(
            unified_name="Inbox",
            folders=[
                UnifiedFolderSource(
                    account_id=uuid.uuid4(),
                    account_name="Acct1",
                    account_emoji="🔵",
                    folder_id=uuid.uuid4(),
                    imap_name="INBOX",
                ),
                UnifiedFolderSource(
                    account_id=uuid.uuid4(),
                    account_name="Acct2",
                    account_emoji="🟢",
                    folder_id=uuid.uuid4(),
                    imap_name="INBOX",
                ),
            ],
            unread_count=15,
            total_count=100,
        )
        assert resp.unified_name == "Inbox"
        assert len(resp.folders) == 2
        assert resp.unread_count == 15
        assert resp.total_count == 100

    def test_unified_message_summary_schema(self) -> None:
        """UnifiedMessageSummary includes account_emoji field."""
        from mail_verdict.api.schemas import UnifiedMessageSummary

        msg = UnifiedMessageSummary(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            account_emoji="📮",
            folder_id=uuid.uuid4(),
            subject="Test",
            from_addr="user@example.com",
            received_at=datetime.now(timezone.utc),
            is_seen=False,
        )
        assert msg.account_emoji == "📮"
        assert not msg.is_seen

    def test_unified_message_summary_nullable_emoji(self) -> None:
        """UnifiedMessageSummary allows null emoji."""
        from mail_verdict.api.schemas import UnifiedMessageSummary

        msg = UnifiedMessageSummary(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            folder_id=uuid.uuid4(),
        )
        assert msg.account_emoji is None

    def test_unified_message_list_response(self) -> None:
        """UnifiedMessageListResponse has pagination fields."""
        from mail_verdict.api.schemas import UnifiedMessageListResponse

        resp = UnifiedMessageListResponse(
            messages=[],
            has_more=False,
            next_cursor=None,
        )
        assert resp.messages == []
        assert not resp.has_more

    def test_emoji_update_schema(self) -> None:
        """EmojiUpdate validates max_length."""
        from mail_verdict.api.schemas import EmojiUpdate

        update = EmojiUpdate(emoji="🎯")
        assert update.emoji == "🎯"

        # Null emoji (clear)
        clear = EmojiUpdate(emoji=None)
        assert clear.emoji is None

    def test_unified_name_update_schema(self) -> None:
        """UnifiedNameUpdate accepts strings and null."""
        from mail_verdict.api.schemas import UnifiedNameUpdate

        update = UnifiedNameUpdate(unified_name="Inbox")
        assert update.unified_name == "Inbox"

        clear = UnifiedNameUpdate(unified_name=None)
        assert clear.unified_name is None

    def test_folder_order_schemas(self) -> None:
        """UnifiedFolderOrderResponse and Update work correctly."""
        from mail_verdict.api.schemas import (
            UnifiedFolderOrderResponse,
            UnifiedFolderOrderUpdate,
        )

        order = UnifiedFolderOrderUpdate(order=["Inbox", "Sent", "Trash"])
        assert order.order == ["Inbox", "Sent", "Trash"]

        resp = UnifiedFolderOrderResponse(order=["Inbox", "Sent"])
        assert len(resp.order) == 2


class TestAccountResponseEmoji:
    """Tests for emoji field in AccountResponse."""

    def test_account_response_has_emoji(self) -> None:
        """AccountResponse includes emoji field."""
        from mail_verdict.api.schemas import AccountResponse

        fields = set(AccountResponse.model_fields.keys())
        assert "emoji" in fields

    def test_account_response_emoji_nullable(self) -> None:
        """AccountResponse emoji defaults to None."""
        from mail_verdict.api.schemas import AccountResponse

        resp = AccountResponse(
            id=uuid.uuid4(),
            name="Test",
            imap_host="imap.example.com",
            imap_port=993,
            imap_user="user@example.com",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        assert resp.emoji is None


class TestFolderResponseUnifiedName:
    """Tests for unified_name field in FolderResponse."""

    def test_folder_response_has_unified_name(self) -> None:
        """FolderResponse includes unified_name field."""
        from mail_verdict.api.schemas import FolderResponse

        fields = set(FolderResponse.model_fields.keys())
        assert "unified_name" in fields

    def test_folder_response_unified_name_nullable(self) -> None:
        """FolderResponse unified_name defaults to None."""
        from mail_verdict.api.schemas import FolderResponse

        resp = FolderResponse(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            imap_name="INBOX",
        )
        assert resp.unified_name is None


class TestUnifiedRouterRegistration:
    """Tests for router registration."""

    def test_unified_routers_in_all_routers(self) -> None:
        """Both unified routers are registered."""
        from mail_verdict.api.routes import all_routers

        prefixes = [r.prefix for r in all_routers]
        assert "/unified" in prefixes
        # Account-scoped router should be in the list
        account_prefixes = [
            p for p in prefixes
            if p.startswith("/accounts/{account_id}")
        ]
        assert len(account_prefixes) >= 2  # folder-management, unified

    def test_unified_router_endpoints(self) -> None:
        """Unified router has expected endpoint paths."""
        from mail_verdict.api.unified import unified_router

        routes = [r.path for r in unified_router.routes]  # type: ignore[union-attr]
        assert any("folders" in r for r in routes)
        assert any("mails" in r or "messages" in r for r in routes)
        assert any("folder-order" in r for r in routes)

    def test_account_router_endpoints(self) -> None:
        """Account-scoped unified router has emoji and unified-name endpoints."""
        from mail_verdict.api.unified import account_router

        routes = [r.path for r in account_router.routes]  # type: ignore[union-attr]
        assert any("emoji" in r for r in routes)
        assert any("unified-name" in r for r in routes)
