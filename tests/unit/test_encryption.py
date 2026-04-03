"""Tests for AES-256-GCM credential encryption: round-trip, key validation, error handling."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from mail_verdict.core.encryption import (
    EncryptionError,
    decrypt,
    encrypt,
    reset_encryption,
    validate_key,
)

HEX_KEY_1 = "a" * 64
HEX_KEY_2 = "b" * 64


@pytest.fixture(autouse=True)
def _reset_encryption_state() -> None:
    """Reset encryption state before each test."""
    reset_encryption()


class TestRoundTrip:
    """Tests for encrypt/decrypt round-trip."""

    def test_encrypt_decrypt_round_trip(self) -> None:
        """Encrypted value can be decrypted back to original."""
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": HEX_KEY_1}):
            plaintext = "my_secret_password"
            ciphertext = encrypt(plaintext)
            assert isinstance(ciphertext, bytes)
            assert ciphertext != plaintext.encode()
            assert decrypt(ciphertext) == plaintext

    def test_different_plaintexts_different_ciphertexts(self) -> None:
        """Different inputs produce different ciphertexts."""
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": HEX_KEY_1}):
            c1 = encrypt("password1")
            c2 = encrypt("password2")
            assert c1 != c2

    def test_unicode_round_trip(self) -> None:
        """Unicode passwords survive encrypt/decrypt."""
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": HEX_KEY_1}):
            plaintext = "p@ssw0rd-mit-ümläuten-🔐"
            assert decrypt(encrypt(plaintext)) == plaintext

    def test_passthrough_mode(self) -> None:
        """Without encryption key, plaintext is stored as UTF-8 bytes."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MAIL_VERDICT_ENCRYPTION_KEY", None)
            ciphertext = encrypt("plaintext_pass")
            assert ciphertext == b"plaintext_pass"
            assert decrypt(ciphertext) == "plaintext_pass"


class TestValidateKey:
    """Tests for key validation on startup."""

    def test_valid_key_passes(self) -> None:
        """Valid 64-char hex key passes validation."""
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": HEX_KEY_1}):
            validate_key()

    def test_missing_key_passthrough(self) -> None:
        """Missing env var enters passthrough mode (no error)."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MAIL_VERDICT_ENCRYPTION_KEY", None)
            validate_key()

    def test_invalid_hex_raises(self) -> None:
        """Non-hex key raises EncryptionError."""
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": "not-hex-" + "x" * 56}):
            with pytest.raises(EncryptionError):
                validate_key()

    def test_wrong_length_raises(self) -> None:
        """Key with wrong length raises EncryptionError."""
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": "aabb"}):
            with pytest.raises(EncryptionError, match="64 hex characters"):
                validate_key()


class TestDecryptErrors:
    """Tests for decryption error handling."""

    def test_wrong_key_raises(self) -> None:
        """Decrypting with wrong key raises EncryptionError."""
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": HEX_KEY_1}):
            ciphertext = encrypt("secret")
        reset_encryption()
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": HEX_KEY_2}):
            with pytest.raises(EncryptionError, match="wrong key"):
                decrypt(ciphertext)

    def test_corrupted_ciphertext_raises(self) -> None:
        """Corrupted ciphertext raises EncryptionError."""
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": HEX_KEY_1}):
            with pytest.raises(EncryptionError):
                decrypt(b"not-valid-ciphertext-at-all-garbage")

    def test_too_short_raises(self) -> None:
        """Ciphertext shorter than IV+tag raises EncryptionError."""
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": HEX_KEY_1}):
            with pytest.raises(EncryptionError, match="too short"):
                decrypt(b"short")
