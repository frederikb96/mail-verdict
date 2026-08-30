"""Tests for ProviderCredentialRepository logic that doesn't require a database.

The DB-backed round trip (store, decrypt, hint) is covered against a real
Postgres in tests/pg/test_provider_credentials.py.
"""

from __future__ import annotations

import pytest

from mail_verdict.settings.credentials import (
    EncryptionUnavailableError,
    ProviderCredentialRepository,
)

_KEY = "0123456789abcdef" * 4


class TestUnknownProvider:
    """An unrecognized provider name is rejected before any DB or crypto work."""

    @pytest.mark.asyncio
    async def test_set_key_rejects_unknown_provider(self) -> None:
        repo = ProviderCredentialRepository(db=None, encryption_key=_KEY)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Unknown provider"):
            await repo.set_key("not-a-real-provider", "value")

    @pytest.mark.asyncio
    async def test_resolve_key_rejects_unknown_provider(self) -> None:
        repo = ProviderCredentialRepository(db=None, encryption_key=_KEY)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Unknown provider"):
            await repo.resolve_key("not-a-real-provider")


class TestNoEncryptionKeyConfigured:
    """Writing a key with no ENCRYPTION_KEY configured fails loudly, not silently."""

    @pytest.mark.asyncio
    async def test_set_key_without_encryption_key_raises(self) -> None:
        repo = ProviderCredentialRepository(db=None, encryption_key="")  # type: ignore[arg-type]
        with pytest.raises(EncryptionUnavailableError):
            await repo.set_key("openai", "sk-test")


class TestEnvironmentFallback:
    """An environment variable is the fallback when nothing is stored (or storable)."""

    @pytest.mark.asyncio
    async def test_resolve_key_falls_back_to_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No encryption key configured -> the DB path is never consulted,
        # so `db=None` never gets touched -- only the env var can answer.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        repo = ProviderCredentialRepository(db=None, encryption_key="")  # type: ignore[arg-type]
        assert await repo.resolve_key("openai") == "sk-from-env"

    @pytest.mark.asyncio
    async def test_resolve_key_none_when_nothing_configured(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        repo = ProviderCredentialRepository(db=None, encryption_key="")  # type: ignore[arg-type]
        assert await repo.resolve_key("anthropic") is None

    @pytest.mark.asyncio
    async def test_status_reports_hint_from_env_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-abcd1234")
        repo = ProviderCredentialRepository(db=None, encryption_key="")  # type: ignore[arg-type]
        status = await repo.status("openai")
        assert status == {"configured": True, "hint": "1234"}
