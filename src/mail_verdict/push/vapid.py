"""
VAPID key management: the server's Web Push signing identity.

Generated the first time it is asked for rather than provisioned --
nothing seeds vapid_keypair, and no chart value or environment variable
carries it. The private key is stored encrypted with the same AES-256-GCM
format core/encryption.py already uses for a provider API key
(settings/credentials.py); the public key is never stored, since it is
cheap to re-derive from the private key and doing so keeps the private
key the pair's one source of truth.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02 as Vapid
from py_vapid.utils import b64urlencode
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mail_verdict.core.encryption import EncryptionError, decrypt, encrypt
from mail_verdict.database.models import VapidKeypair

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)


class VapidUnavailableError(Exception):
    """Raised when the keypair cannot be generated or read -- no
    ENCRYPTION_KEY configured, or (the one path that should never
    actually happen) a just-written row failing to decrypt back. A
    stored key ENCRYPTION_KEY can no longer decrypt is not this: it is
    handled by regenerating, not by raising."""


class VapidKeyRepository:
    """The server's single VAPID identity.

    Cached in memory after the first successful read, unlike a provider
    key (credentials.py deliberately re-decrypts on every call there,
    since that key might be rotated or set for the first time without a
    restart): rotating this one would silently invalidate every existing
    push subscription, so there is no equivalent reason to re-read it,
    and every request sharing one cached Vapid object is what a `Vapid`
    instance passed straight into pywebpush is for in the first place.
    """

    def __init__(self, db: DatabaseConnection, encryption_key: str) -> None:
        self._db = db
        self._encryption_key = encryption_key
        self._cached: Vapid | None = None

    async def get_or_create(self) -> Vapid:
        """
        Return the server's VAPID keypair, generating and storing one the
        first time this is called -- and regenerating it if the stored
        one can no longer be decrypted, the shape an ENCRYPTION_KEY
        rotation takes. There is no way to recover the old private key
        from the wrong ENCRYPTION_KEY, so there is nothing a documented
        manual reset would do that regenerating here does not: every
        subscription signed under the old key is already unusable (its
        applicationServerKey no longer matches what this server would
        sign with), and every device will need to re-enable push either
        way. Regenerating automatically is what turns "push is silently
        broken until someone finds and runs a manual reset" into "push
        recovers itself, loudly, the next time anyone asks for the key".

        Raises:
            VapidUnavailableError: If no ENCRYPTION_KEY is configured
        """
        if self._cached is not None:
            return self._cached
        if not self._encryption_key:
            raise VapidUnavailableError(
                "ENCRYPTION_KEY must be configured to enable push notifications"
            )

        row = await self._read_row()
        pem: str | None = None
        if row is not None:
            try:
                pem = decrypt(row.encrypted_private_key, self._encryption_key)
            except EncryptionError:
                logger.error(
                    "Stored VAPID key could not be decrypted with the current "
                    "ENCRYPTION_KEY (a rotation?) -- regenerating. Every "
                    "existing push subscription is now invalid and every "
                    "device will need to re-enable push notifications."
                )
                async with self._db.session() as session:
                    await session.execute(delete(VapidKeypair).where(VapidKeypair.id == 1))
                row = None

        if row is None:
            vapid = Vapid()
            vapid.generate_keys()
            encrypted = encrypt(vapid.private_pem().decode("ascii"), self._encryption_key)
            async with self._db.session() as session:
                stmt = (
                    pg_insert(VapidKeypair)
                    .values(id=1, encrypted_private_key=encrypted)
                    .on_conflict_do_nothing(index_elements=["id"])
                )
                await session.execute(stmt)
            # Another request may have won the race to generate one --
            # read back whichever row actually exists rather than
            # trusting the keypair generated locally, so every caller
            # ends up signing with the same identity.
            row = await self._read_row()
            assert row is not None  # the insert above guarantees a row exists now
            logger.info("VAPID keypair generated")
            try:
                pem = decrypt(row.encrypted_private_key, self._encryption_key)
            except EncryptionError as exc:
                # Decrypting the row this same call just wrote, with the
                # same key it just encrypted with, failing is not a stale
                # key -- something is wrong with encryption itself.
                raise VapidUnavailableError(
                    "Just-generated VAPID key could not be decrypted back"
                ) from exc

        assert pem is not None
        vapid = Vapid.from_pem(pem.encode("ascii"))
        self._cached = vapid
        return vapid

    async def _read_row(self) -> VapidKeypair | None:
        async with self._db.session() as session:
            result = await session.execute(select(VapidKeypair).where(VapidKeypair.id == 1))
            return result.scalar_one_or_none()

    async def public_key_b64(self) -> str:
        """
        The `applicationServerKey` a browser passes to
        `PushManager.subscribe()`: the public half of the pair as an
        uncompressed EC point, base64url-encoded with no padding.
        """
        vapid = await self.get_or_create()
        raw = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        return str(b64urlencode(raw))


_vapid_repo: VapidKeyRepository | None = None


def init_vapid_key_repo(db: DatabaseConnection, encryption_key: str) -> VapidKeyRepository:
    """Initialize the global VAPID key repository."""
    global _vapid_repo
    _vapid_repo = VapidKeyRepository(db, encryption_key)
    return _vapid_repo


def get_vapid_key_repo() -> VapidKeyRepository:
    """
    Get the global VAPID key repository.

    Raises:
        RuntimeError: If not initialized
    """
    if _vapid_repo is None:
        raise RuntimeError("VapidKeyRepository not initialized")
    return _vapid_repo


def reset_vapid_key_repo() -> None:
    """Reset the global repository. Useful for testing."""
    global _vapid_repo
    _vapid_repo = None
