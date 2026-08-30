"""ProviderCredentialRepository round trips against a real, migrated Postgres."""

from __future__ import annotations

import pytest

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.settings.credentials import ProviderCredentialRepository

_KEY = "0123456789abcdef" * 4
_OTHER_KEY = "fedcba9876543210" * 4


@pytest.fixture(autouse=True)
def _no_ambient_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Unset both provider env vars for every test in this module.

    resolve_key() falls back to the environment when nothing is stored --
    every test here expects that fallback to be absent so a DB-only
    assertion (or an intentionally cleared/undecryptable row) reads as
    None. Without this, a real key present in the ambient environment
    (e.g. for the llm-marked live tests) makes an "expect None" assertion
    fail with the real key value printed into the test output.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


class TestStoreAndResolve:
    """A key stored through set_key is recoverable through resolve_key, encrypted at rest."""

    @pytest.mark.asyncio
    async def test_round_trip(self, migrated_db: DatabaseConnection) -> None:
        repo = ProviderCredentialRepository(migrated_db, _KEY)
        await repo.set_key("openai", "sk-oai-real-value")
        assert await repo.resolve_key("openai") == "sk-oai-real-value"

    @pytest.mark.asyncio
    async def test_stored_at_rest_is_not_plaintext(self, migrated_db: DatabaseConnection) -> None:
        """The bytes actually written to the table never contain the plaintext key."""
        from sqlalchemy import select

        from mail_verdict.database.models import ProviderCredential

        repo = ProviderCredentialRepository(migrated_db, _KEY)
        await repo.set_key("anthropic", "sk-ant-plaintext-marker")

        async with migrated_db.session() as session:
            result = await session.execute(
                select(ProviderCredential).where(ProviderCredential.provider == "anthropic")
            )
            row = result.scalar_one()

        assert b"sk-ant-plaintext-marker" not in row.encrypted_key

    @pytest.mark.asyncio
    async def test_setting_again_replaces_the_key(self, migrated_db: DatabaseConnection) -> None:
        repo = ProviderCredentialRepository(migrated_db, _KEY)
        await repo.set_key("openai", "sk-first")
        await repo.set_key("openai", "sk-second")
        assert await repo.resolve_key("openai") == "sk-second"

    @pytest.mark.asyncio
    async def test_clear_key_removes_it(self, migrated_db: DatabaseConnection) -> None:
        repo = ProviderCredentialRepository(migrated_db, _KEY)
        await repo.set_key("openai", "sk-to-be-cleared")
        await repo.clear_key("openai")
        assert await repo.resolve_key("openai") is None

    @pytest.mark.asyncio
    async def test_status_never_exposes_the_key(self, migrated_db: DatabaseConnection) -> None:
        repo = ProviderCredentialRepository(migrated_db, _KEY)
        await repo.set_key("openai", "sk-abcd1234")
        status = await repo.status("openai")
        assert status["configured"] is True
        assert status["hint"] == "1234"
        assert "sk-abcd1234" not in str(status)


class TestWrongKeyAtRead:
    """A rotated ENCRYPTION_KEY makes previously stored rows unreadable, not corrupted."""

    @pytest.mark.asyncio
    async def test_reading_with_a_different_key_returns_none(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        writer = ProviderCredentialRepository(migrated_db, _KEY)
        await writer.set_key("openai", "sk-original")

        reader = ProviderCredentialRepository(migrated_db, _OTHER_KEY)
        assert await reader.resolve_key("openai") is None
