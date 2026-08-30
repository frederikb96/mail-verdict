"""
Provider API key encryption using AES-256-GCM.

Binary format: [12-byte IV][ciphertext][16-byte auth tag] -- the same shape
PostIMAP uses for its own credential-at-rest encryption, so a deployment
manages one key format rather than two.

The key is passed into every call rather than cached globally: it is read
from infra config at the one call site that owns it (the provider
credential repository), so there is nothing here to grow stale between a
key rotation and a restart.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

IV_LEN = 12
TAG_LEN = 16
KEY_HEX_LEN = 64


class EncryptionError(Exception):
    """Raised when a key is malformed or a decryption fails."""


def _key_bytes(hex_key: str) -> bytes:
    """
    Parse a 64 hex character (32 byte) key.

    Args:
        hex_key: The encryption key as 64 hex characters

    Returns:
        Raw 32-byte key

    Raises:
        EncryptionError: If the key is not 64 hex characters
    """
    if len(hex_key) != KEY_HEX_LEN:
        raise EncryptionError(
            f"encryption key must be {KEY_HEX_LEN} hex characters (32 bytes), "
            f"got {len(hex_key)}"
        )
    try:
        return bytes.fromhex(hex_key)
    except ValueError as exc:
        raise EncryptionError(f"encryption key is not valid hex: {exc}") from exc


def encrypt(plaintext: str, hex_key: str) -> bytes:
    """
    Encrypt a plaintext string with AES-256-GCM.

    Args:
        plaintext: Value to encrypt
        hex_key: 64 hex character (32 byte) key

    Returns:
        IV + ciphertext + auth tag, concatenated
    """
    aesgcm = AESGCM(_key_bytes(hex_key))
    iv = os.urandom(IV_LEN)
    return iv + aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)


def decrypt(ciphertext: bytes, hex_key: str) -> str:
    """
    Decrypt bytes produced by encrypt() back to the plaintext string.

    Args:
        ciphertext: IV + ciphertext + auth tag, as produced by encrypt()
        hex_key: 64 hex character (32 byte) key

    Returns:
        The original plaintext

    Raises:
        EncryptionError: If the ciphertext is malformed or the key is wrong
    """
    if len(ciphertext) < IV_LEN + TAG_LEN:
        raise EncryptionError("ciphertext too short to contain an IV and auth tag")
    aesgcm = AESGCM(_key_bytes(hex_key))
    iv, body = ciphertext[:IV_LEN], ciphertext[IV_LEN:]
    try:
        return aesgcm.decrypt(iv, body, None).decode("utf-8")
    except Exception as exc:
        raise EncryptionError("decryption failed (wrong key or corrupted data)") from exc
