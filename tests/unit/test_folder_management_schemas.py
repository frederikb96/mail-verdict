"""Tests for folder management schemas and router registration."""

from __future__ import annotations

import uuid


class TestFolderOrderSchemas:
    """Tests for folder ordering schemas."""

    def test_folder_order_item_schema(self) -> None:
        """FolderOrderItem has required fields."""
        from mail_verdict.api.schemas import FolderOrderItem

        item = FolderOrderItem(
            folder_id=uuid.uuid4(),
            imap_name="INBOX",
            display_name="Inbox",
            special_use="inbox",
            is_visible=True,
            unread_count=5,
            total_count=100,
        )
        assert item.imap_name == "INBOX"
        assert item.unread_count == 5

    def test_folder_order_item_defaults(self) -> None:
        """FolderOrderItem has correct defaults."""
        from mail_verdict.api.schemas import FolderOrderItem

        item = FolderOrderItem(
            folder_id=uuid.uuid4(),
            imap_name="INBOX",
        )
        assert item.display_name is None
        assert item.special_use is None
        assert item.is_visible is True
        assert item.unread_count == 0
        assert item.total_count == 0

    def test_folder_order_response_schema(self) -> None:
        """FolderOrderResponse holds a list of items."""
        from mail_verdict.api.schemas import FolderOrderItem, FolderOrderResponse

        resp = FolderOrderResponse(
            folders=[
                FolderOrderItem(
                    folder_id=uuid.uuid4(),
                    imap_name="INBOX",
                ),
                FolderOrderItem(
                    folder_id=uuid.uuid4(),
                    imap_name="Sent",
                ),
            ]
        )
        assert len(resp.folders) == 2

    def test_folder_order_update_schema(self) -> None:
        """FolderOrderUpdate accepts a list of UUIDs."""
        from mail_verdict.api.schemas import FolderOrderUpdate

        ids = [uuid.uuid4(), uuid.uuid4()]
        update = FolderOrderUpdate(order=ids)
        assert update.order == ids

    def test_folder_order_update_empty(self) -> None:
        """FolderOrderUpdate accepts empty list."""
        from mail_verdict.api.schemas import FolderOrderUpdate

        update = FolderOrderUpdate(order=[])
        assert update.order == []


class TestFolderPrefsUpdateSchema:
    """Tests for the consolidated folder preferences update schema."""

    def test_accepts_any_single_field(self) -> None:
        """A caller may set just one preference field, leaving the rest unset."""
        from mail_verdict.api.schemas import FolderPrefsUpdate

        visibility_only = FolderPrefsUpdate(is_visible=False)
        assert visibility_only.model_dump(exclude_unset=True) == {"is_visible": False}

        unified_name_only = FolderPrefsUpdate(unified_name="Inbox")
        assert unified_name_only.model_dump(exclude_unset=True) == {"unified_name": "Inbox"}

    def test_unified_name_accepts_null_to_clear(self) -> None:
        """Explicitly setting unified_name to None clears it, distinct from leaving it unset."""
        from mail_verdict.api.schemas import FolderPrefsUpdate

        clear = FolderPrefsUpdate(unified_name=None)
        assert clear.model_dump(exclude_unset=True) == {"unified_name": None}


class TestFolderManagementRouterRegistration:
    """Tests for router registration and endpoint presence."""

    def test_folder_management_router_in_all_routers(self) -> None:
        """Folder management router is registered."""
        from mail_verdict.api.routes import all_routers

        prefixes = [r.prefix for r in all_routers]
        assert any("account" in p for p in prefixes)

    def test_folder_management_endpoints(self) -> None:
        """Folder management router has the folder-order endpoint."""
        from mail_verdict.api.folder_management import router

        routes = [r.path for r in router.routes]  # type: ignore[union-attr]
        assert any("folder-order" in r for r in routes)

    def test_folder_prefs_router_has_prefs_endpoint(self) -> None:
        """The flat /folders/{folder_id}/prefs endpoint replaces visibility/auto-detect."""
        from mail_verdict.api.folder_management import folder_prefs_router

        routes = [r.path for r in folder_prefs_router.routes]  # type: ignore[union-attr]
        assert any("prefs" in r for r in routes)
