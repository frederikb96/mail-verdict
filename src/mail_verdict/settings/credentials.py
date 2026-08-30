"""
Provider API key storage: encrypted in the database, write-only through the API.

A key is settable and reportable as present with a last-four-character
hint; nothing in this module or the API layer built on it ever returns the
key itself. Reads decrypt fresh on every call rather than caching the
plaintext, so rotating a key or setting ENCRYPTION_KEY for the first time
takes effect on the next call, not the next restart. An environment
variable (ANTHROPIC_API_KEY / OPENAI_API_KEY) is the fallback for a
deployment that prefers keeping the key out of the database entirely.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from mail_verdict.core.encryption import EncryptionError, decrypt, encrypt
from mail_verdict.database.models import ProviderCredential

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

PROVIDER_ENV_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


class EncryptionUnavailableError(Exception):
    """Raised when storing a key is attempted with no encryption_key configured."""


def _require_known_provider(provider: str) -> None:
    if provider not in PROVIDER_ENV_VARS:
        raise ValueError(
            f"Unknown provider {provider!r}, expected one of {sorted(PROVIDER_ENV_VARS)}"
        )


class ProviderCredentialRepository:
    """Encrypted CRUD for the provider_credentials table."""

    def __init__(self, db: DatabaseConnection, encryption_key: str) -> None:
        """
        Initialize the repository.

        Args:
            db: Database connection
            encryption_key: 64 hex character AES-256-GCM key from infra
                config, or "" if none is configured -- config is
                restart-only infra, so this is captured once here rather
                than re-read per call the way the key it protects is
        """
        self._db = db
        self._encryption_key = encryption_key

    async def set_key(self, provider: str, plaintext: str) -> None:
        """
        Encrypt and store a provider's API key, replacing any existing one.

        Args:
            provider: "anthropic" or "openai"
            plaintext: The API key to store

        Raises:
            EncryptionUnavailableError: If no encryption_key is configured
        """
        _require_known_provider(provider)
        if not self._encryption_key:
            raise EncryptionUnavailableError(
                "ENCRYPTION_KEY must be configured to store a provider API key"
            )
        encrypted = encrypt(plaintext, self._encryption_key)
        async with self._db.session() as session:
            result = await session.execute(
                select(ProviderCredential).where(ProviderCredential.provider == provider)
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                existing.encrypted_key = encrypted
            else:
                session.add(ProviderCredential(provider=provider, encrypted_key=encrypted))
        logger.info("Provider API key stored", extra={"provider": provider})

    async def clear_key(self, provider: str) -> None:
        """
        Remove a stored provider API key, if any.

        Args:
            provider: "anthropic" or "openai"
        """
        _require_known_provider(provider)
        async with self._db.session() as session:
            await session.execute(
                delete(ProviderCredential).where(ProviderCredential.provider == provider)
            )
        logger.info("Provider API key cleared", extra={"provider": provider})

    async def _get_db_key(self, provider: str) -> str | None:
        """Decrypt the stored key, or None if unset or unreadable."""
        if not self._encryption_key:
            return None
        async with self._db.session() as session:
            result = await session.execute(
                select(ProviderCredential).where(ProviderCredential.provider == provider)
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        try:
            return decrypt(row.encrypted_key, self._encryption_key)
        except EncryptionError:
            logger.exception(
                "Stored provider key could not be decrypted -- wrong ENCRYPTION_KEY?",
                extra={"provider": provider},
            )
            return None

    async def resolve_key(self, provider: str) -> str | None:
        """
        Resolve the API key to actually use for a provider.

        A database-stored key wins; an environment variable is the
        fallback. Read fresh on every call.

        Args:
            provider: "anthropic" or "openai"

        Returns:
            The plaintext key, or None if neither source has one
        """
        _require_known_provider(provider)
        db_key = await self._get_db_key(provider)
        if db_key:
            return db_key
        return os.environ.get(PROVIDER_ENV_VARS[provider]) or None

    async def status(self, provider: str) -> dict[str, str | bool | None]:
        """
        Report whether a provider key is configured, without exposing it.

        Args:
            provider: "anthropic" or "openai"

        Returns:
            {"configured": bool, "hint": last 4 characters, or None}
        """
        key = await self.resolve_key(provider)
        if not key:
            return {"configured": False, "hint": None}
        return {"configured": True, "hint": key[-4:] if len(key) >= 4 else "***"}


_credential_repo: ProviderCredentialRepository | None = None


def init_provider_credential_repo(
    db: DatabaseConnection, encryption_key: str,
) -> ProviderCredentialRepository:
    """Initialize the global provider credential repository."""
    global _credential_repo
    _credential_repo = ProviderCredentialRepository(db, encryption_key)
    return _credential_repo


def get_provider_credential_repo() -> ProviderCredentialRepository:
    """
    Get the global provider credential repository.

    Raises:
        RuntimeError: If not initialized
    """
    if _credential_repo is None:
        raise RuntimeError("ProviderCredentialRepository not initialized")
    return _credential_repo


def reset_provider_credential_repo() -> None:
    """Reset the global repository. Useful for testing."""
    global _credential_repo
    _credential_repo = None
