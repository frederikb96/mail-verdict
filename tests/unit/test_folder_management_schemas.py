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


class TestFolderVisibilitySchemas:
    """Tests for folder visibility schemas."""

    def test_visibility_update(self) -> None:
        """FolderVisibilityUpdate accepts boolean."""
        from mail_verdict.api.schemas import FolderVisibilityUpdate

        show = FolderVisibilityUpdate(is_visible=True)
        assert show.is_visible is True

        hide = FolderVisibilityUpdate(is_visible=False)
        assert hide.is_visible is False

    def test_visibility_response(self) -> None:
        """FolderVisibilityResponse has folder_id and is_visible."""
        from mail_verdict.api.schemas import FolderVisibilityResponse

        resp = FolderVisibilityResponse(
            folder_id=uuid.uuid4(),
            is_visible=False,
        )
        assert resp.is_visible is False


class TestIdleSchemas:
    """Tests for IDLE configuration schemas."""

    def test_idle_folder_item(self) -> None:
        """IdleFolderItem has required fields."""
        from mail_verdict.api.schemas import IdleFolderItem

        item = IdleFolderItem(
            folder_id=uuid.uuid4(),
            imap_name="INBOX",
            idle_enabled=True,
        )
        assert item.idle_enabled is True
        assert item.idle_supported is None

    def test_idle_folder_toggle(self) -> None:
        """IdleFolderToggle has folder_id and enabled."""
        from mail_verdict.api.schemas import IdleFolderToggle

        toggle = IdleFolderToggle(
            folder_id=uuid.uuid4(),
            enabled=True,
        )
        assert toggle.enabled is True

    def test_idle_folder_toggle_response(self) -> None:
        """IdleFolderToggleResponse includes success flag."""
        from mail_verdict.api.schemas import IdleFolderToggleResponse

        resp = IdleFolderToggleResponse(
            folder_id=uuid.uuid4(),
            enabled=False,
            success=True,
        )
        assert resp.success is True
        assert resp.error is None

    def test_idle_validation_request(self) -> None:
        """IdleValidationRequest has folder_id."""
        from mail_verdict.api.schemas import IdleValidationRequest

        req = IdleValidationRequest(folder_id=uuid.uuid4())
        assert req.folder_id is not None

    def test_idle_validation_response(self) -> None:
        """IdleValidationResponse includes supported and error."""
        from mail_verdict.api.schemas import IdleValidationResponse

        resp = IdleValidationResponse(
            folder_id=uuid.uuid4(),
            supported=False,
            error="Server does not support IDLE",
        )
        assert resp.supported is False
        assert resp.error is not None


class TestFolderManagementRouterRegistration:
    """Tests for router registration and endpoint presence."""

    def test_folder_management_router_in_all_routers(self) -> None:
        """Folder management router is registered."""
        from mail_verdict.api.routes import all_routers

        prefixes = [r.prefix for r in all_routers]
        assert any("account" in p for p in prefixes)

    def test_folder_management_endpoints(self) -> None:
        """Folder management router has expected endpoint paths."""
        from mail_verdict.api.folder_management import router

        routes = [r.path for r in router.routes]  # type: ignore[union-attr]
        assert any("folder-order" in r for r in routes)
        assert any("visibility" in r for r in routes)
        assert any("idle-folders" in r for r in routes)
        assert any("validate-idle" in r for r in routes)
        assert any("auto-detect" in r for r in routes)
