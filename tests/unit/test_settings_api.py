"""Tests for Settings API endpoints: GET, PUT, import, provider key write-only handling."""

from __future__ import annotations

import secrets
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mail_verdict.settings.defaults import SETTING_DEFAULTS, SettingCategory


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

    def test_custom_provider_without_base_url_rejected(self, client: TestClient) -> None:
        resp = client.put("/settings/ai", json={"data": {"provider": "custom"}})
        assert resp.status_code == 400

    def test_custom_provider_with_base_url_accepted(self, client: TestClient) -> None:
        resp = client.put(
            "/settings/ai",
            json={"data": {"provider": "custom", "base_url": "https://example.test/v1"}},
        )
        assert resp.status_code == 200

    def test_custom_api_key_can_be_stored(self, client: TestClient) -> None:
        """The 'custom' provider gets its own credential slot, the same
        write-only shape as anthropic/openai."""
        resp = client.put(
            "/settings/ai", json={"data": {"custom_api_key": "sk-custom-secret"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "sk-custom-secret" not in resp.text
        assert data["custom_api_key_configured"] is True


class TestSemanticSettings:
    """Semantic category: provider validation and the active-model freeze
    that keeps search on the old vector space during a re-embed."""

    def test_custom_provider_without_base_url_rejected(self, client: TestClient) -> None:
        resp = client.put("/settings/semantic", json={"data": {"provider": "custom"}})
        assert resp.status_code == 400

    def test_anthropic_provider_rejected(self, client: TestClient) -> None:
        """No embedding model of Anthropic's own -- never a valid choice."""
        resp = client.put("/settings/semantic", json={"data": {"provider": "anthropic"}})
        assert resp.status_code == 400

    def test_changing_model_freezes_the_previously_active_identity(
        self, client: TestClient,
    ) -> None:
        before = client.get("/settings/semantic").json()
        old_model = before["model"]
        old_provider = before["provider"]

        resp = client.put("/settings/semantic", json={"data": {"model": "new-embedding-model"}})
        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "new-embedding-model"
        assert data["active_model"] == old_model
        assert data["active_provider"] == old_provider

    def test_a_second_change_before_cutover_keeps_the_original_frozen_identity(
        self, client: TestClient,
    ) -> None:
        """Search must keep answering from whatever completed last time,
        not from a target that was itself never finished."""
        before = client.get("/settings/semantic").json()
        original_model = before["model"]

        client.put("/settings/semantic", json={"data": {"model": "first-new-model"}})
        resp = client.put("/settings/semantic", json={"data": {"model": "second-new-model"}})

        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "second-new-model"
        assert data["active_model"] == original_model

    def test_rewriting_the_same_model_does_not_disturb_an_unset_active_model(
        self, client: TestClient,
    ) -> None:
        before = client.get("/settings/semantic").json()
        resp = client.put("/settings/semantic", json={"data": {"model": before["model"]}})
        assert resp.status_code == 200
        assert resp.json()["active_model"] is None

    def test_import_also_freezes_the_active_identity(self, client: TestClient) -> None:
        """The mock's bulk_import returns a canned snapshot rather than
        reflecting what it was called with (unlike the real service, see
        settings/service.py), so this asserts on the call args -- the
        same reason test_round_tripping_a_get_response_never_reaches_the_
        settings_store above does for update()."""
        from mail_verdict.api import settings_api as settings_api_module

        before = client.get("/settings/semantic").json()
        old_model = before["model"]

        service = settings_api_module.get_settings_service()
        resp = client.post(
            "/settings/import",
            json={"data": {"semantic": {"model": "imported-new-model"}}},
        )
        assert resp.status_code == 200
        imported = service.bulk_import.await_args.args[0]  # type: ignore[union-attr]
        assert imported["semantic"]["active_model"] == old_model

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


class TestCredentialMaskingAppliesToEveryCategory:
    """
    A provider key set through the settings API must never be readable
    back out of it, whichever category it belongs to -- not just "ai".

    Each test here generates its own throwaway value (never a real
    credential) and asserts no field of any response equals it.
    """

    @pytest.mark.parametrize("category", [cat.value for cat in SettingCategory])
    def test_a_key_sent_to_any_category_never_reaches_a_read(
        self, client: TestClient, category: str,
    ) -> None:
        """
        The generic PUT endpoint takes an arbitrary dict for every
        category, so a key-shaped field reaching one that has no concept
        of a provider (retry, pipeline, calendar, outbox, mail, orders)
        must be masked exactly the same as it is for "ai" -- matched by
        the field's shape, not by which category asked for it.
        """
        secret_value = secrets.token_hex(16)
        put_resp = client.put(
            f"/settings/{category}", json={"data": {"openai_api_key": secret_value}},
        )
        assert put_resp.status_code == 200, put_resp.text
        assert secret_value not in put_resp.text

        get_resp = client.get(f"/settings/{category}")
        assert get_resp.status_code == 200
        assert secret_value not in get_resp.text
        assert "openai_api_key" not in get_resp.json()

    def test_semantic_custom_api_key_never_returns_it(
        self, client: TestClient, cred_repo: MagicMock,
    ) -> None:
        """
        The actual production shape: the "custom" provider's key is
        shared between "ai" and "semantic" (settings/credentials.py), so
        setting it while configuring semantic search for a custom,
        OpenAI-compatible embeddings server must store it the same way
        the "ai" category does -- never merge it into semantic's own
        JSONB blob, never return it on the write's own response or on
        any later read.
        """
        secret_value = f"sk-{secrets.token_hex(16)}"
        put_resp = client.put(
            "/settings/semantic",
            json={
                "data": {
                    "provider": "custom",
                    "base_url": "https://example.test/v1",
                    "custom_api_key": secret_value,
                },
            },
        )
        assert put_resp.status_code == 200, put_resp.text
        assert secret_value not in put_resp.text
        assert "custom_api_key" not in put_resp.json()

        get_resp = client.get("/settings/semantic")
        assert get_resp.status_code == 200
        assert secret_value not in get_resp.text
        assert "custom_api_key" not in get_resp.json()

        # Routed to the shared credential store, not merely dropped.
        assert cred_repo._stored.get("custom") == secret_value

    def test_credential_shaped_field_for_an_unknown_provider_is_dropped(
        self, client: TestClient, cred_repo: MagicMock,
    ) -> None:
        """
        A field shaped like a key but naming a provider this server has
        no credential slot for is never stored anywhere -- silently
        dropped rather than persisted in the clear or raising.
        """
        secret_value = secrets.token_hex(16)
        resp = client.put(
            "/settings/ai", json={"data": {"mistral_api_key": secret_value}},
        )
        assert resp.status_code == 200
        assert secret_value not in resp.text
        cred_repo.set_key.assert_not_awaited()

    def test_write_response_and_a_later_read_agree(self, client: TestClient) -> None:
        """The write's own answer and a subsequent read are masked the
        same way -- one code path, not two that could drift apart."""
        secret_value = secrets.token_hex(16)
        put_resp = client.put(
            "/settings/pipeline", json={"data": {"openai_api_key": secret_value}},
        )
        get_resp = client.get("/settings/pipeline")
        assert put_resp.json() == get_resp.json()


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

    def test_import_never_stores_a_key_sent_to_any_category(
        self, client: TestClient, cred_repo: MagicMock,
    ) -> None:
        """The same masking as a PUT, for import -- a key-shaped field
        under a category other than "ai" is dropped too, never merged
        into that category's JSONB blob.

        The mock's bulk_import returns a canned snapshot rather than
        reflecting what it was called with (see
        test_import_also_freezes_the_active_identity's docstring above),
        so this asserts on the call args, the same way that test does.
        """
        from mail_verdict.api import settings_api as settings_api_module

        secret_value = secrets.token_hex(16)
        resp = client.post("/settings/import", json={
            "data": {"semantic": {"custom_api_key": secret_value}},
        })
        assert resp.status_code == 200

        service = settings_api_module.get_settings_service()
        imported = service.bulk_import.await_args.args[0]  # type: ignore[union-attr]
        assert "custom_api_key" not in imported["semantic"]
        cred_repo.set_key.assert_not_awaited()
