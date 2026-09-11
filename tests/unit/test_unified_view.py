"""Tests for unified view: schemas, model fields, and API schema validation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


class TestUnifiedViewModels:
    """Tests for unified view model columns (PostIMAP + Prefs split)."""

    def test_unified_view_membership_is_many_to_many(self) -> None:
        """A view is its own row and membership is keyed by (view, folder),
        so one folder can belong to several views."""
        from mail_verdict.database.models import UnifiedView, UnifiedViewFolder

        primary_key = {c.name for c in UnifiedViewFolder.__table__.primary_key}
        assert primary_key == {"view_id", "folder_id"}
        assert hasattr(UnifiedView, "emoji")

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
            id=uuid.uuid4(),
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

    def test_emoji_update_schema(self) -> None:
        """EmojiUpdate validates max_length."""
        from mail_verdict.api.schemas import EmojiUpdate

        update = EmojiUpdate(emoji="🎯")
        assert update.emoji == "🎯"

        # Null emoji (clear)
        clear = EmojiUpdate(emoji=None)
        assert clear.emoji is None

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


class TestFolderResponseUnifiedViews:
    """A folder answers with every unified view it belongs to."""

    def test_folder_response_lists_its_views(self) -> None:
        from mail_verdict.api.schemas import FolderResponse

        view_ids = [uuid.uuid4(), uuid.uuid4()]
        resp = FolderResponse(
            id=uuid.uuid4(), account_id=uuid.uuid4(), imap_name="INBOX",
            unified_view_ids=view_ids,
        )
        assert resp.unified_view_ids == view_ids

    def test_folder_response_defaults_to_no_views(self) -> None:
        from mail_verdict.api.schemas import FolderResponse

        resp = FolderResponse(id=uuid.uuid4(), account_id=uuid.uuid4(), imap_name="INBOX")
        assert resp.unified_view_ids == []


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
        """Account-scoped unified router has the emoji endpoint.

        A folder's unified view membership is set via folder_management.folder_prefs_router
        instead -- one write surface for every folder preference.
        """
        from mail_verdict.api.unified import account_router

        routes = [r.path for r in account_router.routes]  # type: ignore[union-attr]
        assert any("emoji" in r for r in routes)
