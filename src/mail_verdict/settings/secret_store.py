"""
Named secrets: encrypted in the database, write-only through the API.

The same AES-256-GCM scheme and ENCRYPTION_KEY as provider_credentials
(settings/credentials.py). Nothing here or in the API built on it returns a
value; the one reader of values is the webhook delivery worker, through
`resolve_many`, which decrypts fresh on every call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mail_verdict.core.encryption import EncryptionError, decrypt, encrypt
from mail_verdict.database.models import Secret
from mail_verdict.settings.credentials import EncryptionUnavailableError
from mail_verdict.webhooks.spec import SECRET_NAME

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)


class SecretUnavailableError(Exception):
    """A referenced secret is not stored, or cannot be decrypted. Carries
    the secret's name only."""


@dataclass(frozen=True)
class SecretInfo:
    """What the API may say about a secret: never its value."""

    name: str
    created_at: datetime
    updated_at: datetime


def _require_valid_name(name: str) -> None:
    if not SECRET_NAME.match(name):
        raise ValueError("secret name must start with a letter and use letters, digits or '_'")


class SecretRepository:
    """Encrypted CRUD for the secrets table."""

    def __init__(self, db: DatabaseConnection, encryption_key: str) -> None:
        """
        Args:
            db: Database connection
            encryption_key: 64 hex character AES-256-GCM key from infra
                config, or "" when none is configured
        """
        self._db = db
        self._encryption_key = encryption_key

    async def put(self, name: str, value: str) -> bool:
        """
        Store a secret, replacing any existing one of that name.

        Returns:
            True when the name is new, False when it replaced a value

        Raises:
            ValueError: the name is not a valid secret name or the value is empty
            EncryptionUnavailableError: no encryption key is configured
        """
        _require_valid_name(name)
        if not value:
            raise ValueError("a secret's value must not be empty")
        if not self._encryption_key:
            raise EncryptionUnavailableError(
                "ENCRYPTION_KEY must be configured to store a secret"
            )
        encrypted = encrypt(value, self._encryption_key)
        async with self._db.session() as session:
            existed = (
                await session.execute(select(Secret.name).where(Secret.name == name))
            ).scalar_one_or_none() is not None
            stmt = pg_insert(Secret).values(name=name, encrypted_value=encrypted)
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[Secret.name],
                    set_={"encrypted_value": encrypted, "updated_at": func.now()},
                )
            )
        logger.info("Secret stored", extra={"secret": name})
        return not existed

    async def delete(self, name: str) -> bool:
        """Remove a secret. Returns whether one existed."""
        async with self._db.session() as session:
            result = await session.execute(delete(Secret).where(Secret.name == name))
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def list_all(self) -> list[SecretInfo]:
        """Every stored secret's name and timestamps, by name."""
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(Secret.name, Secret.created_at, Secret.updated_at).order_by(Secret.name)
                )
            ).all()
        return [SecretInfo(r.name, r.created_at, r.updated_at) for r in rows]

    async def names(self) -> set[str]:
        """The set of stored names."""
        return {info.name for info in await self.list_all()}

    async def resolve_many(self, names: Iterable[str]) -> dict[str, str]:
        """
        Decrypt the named secrets, fresh.

        Raises:
            SecretUnavailableError: a name is not stored, or its value
                cannot be decrypted with the configured key
        """
        wanted = list(dict.fromkeys(names))
        if not wanted:
            return {}
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(Secret.name, Secret.encrypted_value).where(Secret.name.in_(wanted))
                )
            ).all()
        stored = {r.name: r.encrypted_value for r in rows}
        resolved: dict[str, str] = {}
        for name in wanted:
            if name not in stored:
                raise SecretUnavailableError(f"secret {name!r} is not set")
            if not self._encryption_key:
                raise SecretUnavailableError(f"secret {name!r} cannot be read: no ENCRYPTION_KEY")
            try:
                resolved[name] = decrypt(stored[name], self._encryption_key)
            except EncryptionError as exc:
                raise SecretUnavailableError(
                    f"secret {name!r} cannot be decrypted -- wrong ENCRYPTION_KEY?"
                ) from exc
        return resolved


_secret_repo: SecretRepository | None = None


def init_secret_repo(db: DatabaseConnection, encryption_key: str) -> SecretRepository:
    """Initialize the global secret repository."""
    global _secret_repo
    _secret_repo = SecretRepository(db, encryption_key)
    return _secret_repo


def get_secret_repo() -> SecretRepository:
    """Get the global secret repository.

    Raises:
        RuntimeError: If not initialized
    """
    if _secret_repo is None:
        raise RuntimeError("SecretRepository not initialized")
    return _secret_repo


def reset_secret_repo() -> None:
    """Reset the global repository. Useful for testing."""
    global _secret_repo
    _secret_repo = None
