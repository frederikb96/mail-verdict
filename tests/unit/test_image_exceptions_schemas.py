"""Tests for image exception schemas and router registration."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError


class TestImageExceptionSchemas:
    """Tests for image exception Pydantic schemas."""

    def test_image_exception_create_sender(self) -> None:
        """ImageExceptionCreate accepts 'sender' type."""
        from mail_verdict.api.schemas import ImageExceptionCreate

        exc = ImageExceptionCreate(type="sender", value="user@example.com")
        assert exc.type == "sender"
        assert exc.value == "user@example.com"

    def test_image_exception_create_domain(self) -> None:
        """ImageExceptionCreate accepts 'domain' type."""
        from mail_verdict.api.schemas import ImageExceptionCreate

        exc = ImageExceptionCreate(type="domain", value="example.com")
        assert exc.type == "domain"

    def test_image_exception_create_invalid_type(self) -> None:
        """ImageExceptionCreate rejects invalid types."""
        from mail_verdict.api.schemas import ImageExceptionCreate

        with pytest.raises(ValidationError):
            ImageExceptionCreate(type="invalid", value="test")

    def test_image_exception_response(self) -> None:
        """ImageExceptionResponse has all fields."""
        from mail_verdict.api.schemas import ImageExceptionResponse

        resp = ImageExceptionResponse(
            id=uuid.uuid4(),
            type="sender",
            value="user@example.com",
            created_at=datetime.now(timezone.utc),
        )
        assert resp.type == "sender"
        assert resp.value == "user@example.com"


class TestImageExceptionRouterRegistration:
    """Tests for router registration."""

    def test_image_exceptions_router_in_all_routers(self) -> None:
        """Image exceptions router is registered."""
        from mail_verdict.api.routes import all_routers

        prefixes = [r.prefix for r in all_routers]
        assert any("image-exceptions" in p for p in prefixes)

    def test_image_exceptions_router_endpoints(self) -> None:
        """Image exceptions router has CRUD and check endpoints."""
        from mail_verdict.api.image_exceptions import router

        routes = [r.path for r in router.routes]  # type: ignore[union-attr]
        # Should have list/create (empty path), delete, and check
        assert any("check" in r for r in routes)
        assert any("{exception_id}" in r for r in routes)


class TestImageExceptionTypeEnum:
    """Tests for ImageExceptionType enum."""

    def test_enum_values(self) -> None:
        """ImageExceptionType has sender and domain values."""
        from mail_verdict.database.models import ImageExceptionType

        assert ImageExceptionType.SENDER.value == "sender"
        assert ImageExceptionType.DOMAIN.value == "domain"

    def test_enum_count(self) -> None:
        """ImageExceptionType has exactly two values."""
        from mail_verdict.database.models import ImageExceptionType

        assert len(ImageExceptionType) == 2
