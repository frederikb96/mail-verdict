"""
Credential encryption using Fernet symmetric encryption.

Encrypts IMAP/SMTP passwords at rest in the database.
Requires MAIL_VERDICT_ENCRYPTION_KEY environment variable.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

ENV_KEY = "MAIL_VERDICT_ENCRYPTION_KEY"

_fernet: Fernet | None = None


class EncryptionError(Exception):
    """Raised when encryption/decryption fails."""


def _get_fernet() -> Fernet:
    """
    Get or create the Fernet instance from the encryption key env var.

    Raises:
        EncryptionError: If the encryption key is not set or invalid
    """
    global _fernet
    if _fernet is None:
        key = os.environ.get(ENV_KEY)
        if not key:
            raise EncryptionError(
                f"{ENV_KEY} environment variable is required. "
                f"Generate one with: python -c \"from cryptography.fernet import Fernet; "
                f"print(Fernet.generate_key().decode())\""
            )
        try:
            _fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, Exception) as e:
            raise EncryptionError(f"Invalid encryption key in {ENV_KEY}: {e}") from e
    return _fernet


def encrypt(plaintext: str) -> str:
    """
    Encrypt a plaintext string.

    Args:
        plaintext: Value to encrypt

    Returns:
        Base64-encoded encrypted string
    """
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """
    Decrypt an encrypted string.

    Args:
        ciphertext: Base64-encoded encrypted value

    Returns:
        Decrypted plaintext string

    Raises:
        EncryptionError: If decryption fails (wrong key or corrupted data)
    """
    f = _get_fernet()
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise EncryptionError("Failed to decrypt value (wrong key or corrupted data)") from e


def validate_key() -> None:
    """
    Validate that the encryption key is set and functional.

    Should be called at app startup.

    Raises:
        EncryptionError: If key is missing or invalid
    """
    f = _get_fernet()
    test = f.encrypt(b"test")
    f.decrypt(test)


def reset_encryption() -> None:
    """Reset the cached Fernet instance. Useful for testing."""
    global _fernet
    _fernet = None
