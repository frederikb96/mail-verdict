"""
Credential encryption using AES-256-GCM.

Binary format: [12-byte IV][ciphertext][16-byte auth tag]
Compatible with PostIMAP's crypto.ts (same format, shared key).

Key: 64 hex characters (32 bytes) from MAIL_VERDICT_ENCRYPTION_KEY env var.
Passthrough mode when no key is set (stores plaintext as bytes).
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENV_KEY = "MAIL_VERDICT_ENCRYPTION_KEY"
IV_LEN = 12
TAG_LEN = 16

_key_bytes: bytes | None = None
_key_loaded = False


class EncryptionError(Exception):
    """Raised when encryption/decryption fails."""


def _get_key() -> bytes | None:
    """Load the hex key from env var. Returns None if not set (passthrough mode)."""
    global _key_bytes, _key_loaded
    if not _key_loaded:
        hex_key = os.environ.get(ENV_KEY, "").strip()
        if hex_key:
            if len(hex_key) != 64:
                raise EncryptionError(
                    f"{ENV_KEY} must be 64 hex characters (32 bytes), got {len(hex_key)}"
                )
            try:
                _key_bytes = bytes.fromhex(hex_key)
            except ValueError as e:
                raise EncryptionError(f"Invalid hex in {ENV_KEY}: {e}") from e
        else:
            _key_bytes = None
        _key_loaded = True
    return _key_bytes


def encrypt(plaintext: str) -> bytes:
    """Encrypt a plaintext string to bytes using AES-256-GCM.

    Returns [12-byte IV][ciphertext][16-byte tag] as bytes.
    In passthrough mode (no key), returns plaintext encoded as UTF-8.
    """
    key = _get_key()
    if key is None:
        return plaintext.encode("utf-8")
    aesgcm = AESGCM(key)
    iv = os.urandom(IV_LEN)
    ct_with_tag = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)
    return iv + ct_with_tag


def decrypt(ciphertext: bytes) -> str:
    """Decrypt bytes back to a plaintext string.

    Expects [12-byte IV][ciphertext][16-byte tag] format.
    In passthrough mode (no key), returns bytes decoded as UTF-8.
    """
    key = _get_key()
    if key is None:
        return ciphertext.decode("utf-8")
    if len(ciphertext) < IV_LEN + TAG_LEN:
        raise EncryptionError("Ciphertext too short to contain IV + auth tag")
    iv = ciphertext[:IV_LEN]
    ct_with_tag = ciphertext[IV_LEN:]
    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(iv, ct_with_tag, None)
    except Exception as e:
        raise EncryptionError("Decryption failed (wrong key or corrupted data)") from e
    return plaintext.decode("utf-8")


def validate_key() -> None:
    """Validate encryption key with a round-trip test. Called at app startup."""
    key = _get_key()
    if key is None:
        return
    test = "validation-test"
    encrypted = encrypt(test)
    decrypted = decrypt(encrypted)
    if decrypted != test:
        raise EncryptionError("Encryption key validation failed: round-trip mismatch")


def reset_encryption() -> None:
    """Reset cached key. Useful for testing."""
    global _key_bytes, _key_loaded
    _key_bytes = None
    _key_loaded = False
