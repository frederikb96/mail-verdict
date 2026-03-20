"""Tests for Settings API endpoints: GET, PUT, import."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mail_verdict.settings.defaults import SETTING_DEFAULTS


def _make_mock_service(
    settings: dict[str, dict[str, Any]] | None = None,
) -> MagicMock:
    """Create a mock SettingsService."""
    service = MagicMock()
    data = settings or {cat.value: dict(v) for cat, v in SETTING_DEFAULTS.items()}
    service.get = MagicMock(side_effect=lambda cat: dict(data.get(cat, {})))
    service.get_all = MagicMock(return_value=data)
    service.update = AsyncMock(side_effect=lambda cat, d: {**data.get(cat, {}), **d})
    service.bulk_import = AsyncMock(return_value=data)
    return service


@pytest.fixture()
def client() -> TestClient:
    """Create a test client with mocked settings service."""
    from fastapi import FastAPI

    from mail_verdict.api.settings_api import router

    app = FastAPI()
    app.include_router(router)

    mock_service = _make_mock_service()
    with patch("mail_verdict.api.settings_api.get_settings_service", return_value=mock_service):
        yield TestClient(app)


class TestGetSettings:
    """Tests for GET /api/settings endpoints."""

    def test_get_all_settings(self, client: TestClient) -> None:
        """GET /api/settings returns all categories."""
        resp = client.get("/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)

    def test_get_single_category(self, client: TestClient) -> None:
        """GET /api/settings/ai returns AI settings."""
        resp = client.get("/settings/ai")
        assert resp.status_code == 200
        data = resp.json()
        assert "model" in data

    def test_get_invalid_category_returns_400(self, client: TestClient) -> None:
        """GET /api/settings/invalid returns 400."""
        resp = client.get("/settings/invalid")
        assert resp.status_code == 400


class TestUpdateSettings:
    """Tests for PUT /api/settings/{category}."""

    def test_update_valid_category(self, client: TestClient) -> None:
        """PUT /api/settings/ai updates AI settings."""
        resp = client.put("/settings/ai", json={"data": {"model": "new-model"}})
        assert resp.status_code == 200

    def test_update_invalid_category_returns_400(self, client: TestClient) -> None:
        """PUT /api/settings/bogus returns 400."""
        resp = client.put("/settings/bogus", json={"data": {"key": "val"}})
        assert resp.status_code == 400


class TestImportSettings:
    """Tests for POST /api/settings/import."""

    def test_import_valid_data(self, client: TestClient) -> None:
        """POST /api/settings/import accepts valid categories."""
        resp = client.post("/settings/import", json={
            "data": {
                "ai": {"model": "imported-model"},
                "spam": {"enabled": False},
            },
        })
        assert resp.status_code == 200

    def test_import_invalid_category_returns_400(self, client: TestClient) -> None:
        """POST /api/settings/import rejects invalid categories."""
        resp = client.post("/settings/import", json={
            "data": {"invalid_cat": {"key": "val"}},
        })
        assert resp.status_code == 400
