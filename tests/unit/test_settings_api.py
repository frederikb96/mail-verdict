"""Tests for Settings API endpoints: GET, PUT, import, provider key write-only handling."""

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

    async def _update(cat: str, d: dict[str, Any]) -> dict[str, Any]:
        data[cat] = {**data.get(cat, {}), **d}
        return dict(data[cat])

    service.update = AsyncMock(side_effect=_update)
    service.bulk_import = AsyncMock(return_value=data)
    return service


def _make_mock_cred_repo() -> MagicMock:
    """Create a mock ProviderCredentialRepository with in-memory storage."""
    repo = MagicMock()
    stored: dict[str, str] = {}

    async def _set_key(provider: str, plaintext: str) -> None:
        stored[provider] = plaintext

    async def _clear_key(provider: str) -> None:
        stored.pop(provider, None)

    async def _status(provider: str) -> dict[str, Any]:
        key = stored.get(provider)
        if not key:
            return {"configured": False, "hint": None}
        return {"configured": True, "hint": key[-4:]}

    repo.set_key = AsyncMock(side_effect=_set_key)
    repo.clear_key = AsyncMock(side_effect=_clear_key)
    repo.status = AsyncMock(side_effect=_status)
    repo._stored = stored
    return repo


@pytest.fixture()
def cred_repo() -> MagicMock:
    """The mock ProviderCredentialRepository backing the `client` fixture."""
    return _make_mock_cred_repo()


@pytest.fixture()
def client(cred_repo: MagicMock) -> TestClient:
    """Create a test client with mocked settings service and credential repo."""
    from fastapi import FastAPI

    from mail_verdict.api.settings_api import router

    app = FastAPI()
    app.include_router(router)

    mock_service = _make_mock_service()
    with (
        patch("mail_verdict.api.settings_api.get_settings_service", return_value=mock_service),
        patch(
            "mail_verdict.api.settings_api.get_provider_credential_repo",
            return_value=cred_repo,
        ),
    ):
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

    def test_ai_category_reports_credential_status_not_the_key(
        self, client: TestClient,
    ) -> None:
        """GET /api/settings/ai reports presence + hint, never the key itself."""
        resp = client.get("/settings/ai")
        data = resp.json()
        assert data["anthropic_api_key_configured"] is False
        assert data["anthropic_api_key_hint"] is None
        assert "anthropic_api_key" not in data


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

    def test_setting_provider_api_key_never_returns_it(self, client: TestClient) -> None:
        """A key sent on PUT is stored, not merged back into the response."""
        resp = client.put(
            "/settings/ai", json={"data": {"openai_api_key": "sk-super-secret-value"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "sk-super-secret-value" not in resp.text
        assert data["openai_api_key_configured"] is True
        assert data["openai_api_key_hint"] == "alue"

    def test_provider_api_key_never_written_into_settings_blob(self, client: TestClient) -> None:
        """The raw settings category never gets a plaintext key merged in."""
        client.put("/settings/ai", json={"data": {"openai_api_key": "sk-super-secret-value"}})
        resp = client.get("/settings/ai")
        assert "openai_api_key" not in resp.json()

    def test_empty_key_clears_it(self, client: TestClient) -> None:
        """Setting a provider key to an empty string clears it."""
        client.put("/settings/ai", json={"data": {"openai_api_key": "sk-a-real-key"}})
        client.put("/settings/ai", json={"data": {"openai_api_key": ""}})
        resp = client.get("/settings/ai")
        assert resp.json()["openai_api_key_configured"] is False

    def test_invalid_reasoning_effort_for_provider_rejected(self, client: TestClient) -> None:
        """An effort level the selected provider doesn't support is a 400, not stored silently."""
        resp = client.put(
            "/settings/ai",
            json={"data": {"provider": "anthropic", "reasoning_effort": "not-a-real-level"}},
        )
        assert resp.status_code == 400

    def test_unknown_provider_rejected(self, client: TestClient) -> None:
        resp = client.put("/settings/ai", json={"data": {"provider": "not-a-real-provider"}})
        assert resp.status_code == 400

    def test_wrongly_typed_value_on_a_non_ai_category_is_a_400(self, client: TestClient) -> None:
        """
        The ai category's own validate_ai_settings() is caught explicitly,
        but every other category's type check (SettingsService._validate_types,
        raised from inside service.update()) needs the same 400, not an
        unhandled 500 for a mistake this obviously the caller's.
        """
        from mail_verdict.api import settings_api as settings_api_module

        service = settings_api_module.get_settings_service()
        service.update.side_effect = ValueError("Setting 'retry.max_retries' expects int")

        resp = client.put("/settings/retry", json={"data": {"max_retries": "banana"}})
        assert resp.status_code == 400

    def test_round_tripping_a_get_response_never_reaches_the_settings_store(
        self, client: TestClient,
    ) -> None:
        """
        A client that PUTs back what GET returned can't write the computed
        status fields into the underlying settings store.

        GET always recomputes these fields regardless of what's stored, so
        asserting on a follow-up GET would pass even with no stripping at
        all -- assert directly on what reached the mocked store instead.
        """
        from mail_verdict.api import settings_api as settings_api_module

        fetched = client.get("/settings/ai").json()
        assert "openai_api_key_configured" in fetched  # sanity: the field really is there to strip

        service = settings_api_module.get_settings_service()
        resp = client.put("/settings/ai", json={"data": fetched})
        assert resp.status_code == 200

        stored_data = service.update.await_args.args[1]  # type: ignore[union-attr]
        assert "openai_api_key_configured" not in stored_data
        assert "openai_api_key_hint" not in stored_data
        assert "anthropic_api_key_configured" not in stored_data
        assert "anthropic_api_key_hint" not in stored_data


class TestImportSettings:
    """Tests for POST /api/settings/import."""

    def test_import_valid_data(self, client: TestClient) -> None:
        """POST /api/settings/import accepts valid categories."""
        resp = client.post("/settings/import", json={
            "data": {
                "ai": {"model": "imported-model"},
                "retry": {"max_retries": 3},
            },
        })
        assert resp.status_code == 200

    def test_import_rejects_a_wrongly_typed_value(self, client: TestClient) -> None:
        """
        A ValueError raised by the settings service's own type validation
        (real behaviour covered in test_settings_service.py) must surface
        through this endpoint as a 400, not an unhandled 500.
        """
        from mail_verdict.api import settings_api as settings_api_module

        service = settings_api_module.get_settings_service()
        service.bulk_import.side_effect = ValueError("Setting 'retry.max_retries' expects int")

        resp = client.post("/settings/import", json={
            "data": {"retry": {"max_retries": "banana"}},
        })
        assert resp.status_code == 400

    def test_import_invalid_category_returns_400(self, client: TestClient) -> None:
        """POST /api/settings/import rejects invalid categories."""
        resp = client.post("/settings/import", json={
            "data": {"invalid_cat": {"key": "val"}},
        })
        assert resp.status_code == 400

    def test_import_never_writes_a_provider_key(
        self, client: TestClient, cred_repo: MagicMock,
    ) -> None:
        """A key field slipped into an import payload never reaches the credential store."""
        resp = client.post("/settings/import", json={
            "data": {"ai": {"openai_api_key": "sk-should-not-be-stored"}},
        })
        assert resp.status_code == 200
        cred_repo.set_key.assert_not_awaited()
