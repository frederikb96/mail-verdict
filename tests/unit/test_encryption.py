"""Tests for Fernet credential encryption: round-trip, key validation, error handling."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from mail_verdict.core.encryption import (
    EncryptionError,
    decrypt,
    encrypt,
    reset_encryption,
    validate_key,
)


@pytest.fixture(autouse=True)
def _reset_encryption_state() -> None:
    """Reset encryption state before each test."""
    reset_encryption()


class TestRoundTrip:
    """Tests for encrypt/decrypt round-trip."""

    def test_encrypt_decrypt_round_trip(self) -> None:
        """Encrypted value can be decrypted back to original."""
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": key}):
            plaintext = "my_secret_password"
            ciphertext = encrypt(plaintext)
            assert ciphertext != plaintext
            assert decrypt(ciphertext) == plaintext

    def test_different_plaintexts_different_ciphertexts(self) -> None:
        """Different inputs produce different ciphertexts."""
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": key}):
            c1 = encrypt("password1")
            c2 = encrypt("password2")
            assert c1 != c2

    def test_unicode_round_trip(self) -> None:
        """Unicode passwords survive encrypt/decrypt."""
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": key}):
            plaintext = "p@ssw0rd-mit-umlauten"
            assert decrypt(encrypt(plaintext)) == plaintext


class TestValidateKey:
    """Tests for key validation on startup."""

    def test_valid_key_passes(self) -> None:
        """Valid Fernet key passes validation."""
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": key}):
            validate_key()

    def test_missing_key_raises(self) -> None:
        """Missing env var raises EncryptionError."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MAIL_VERDICT_ENCRYPTION_KEY", None)
            with pytest.raises(EncryptionError, match="MAIL_VERDICT_ENCRYPTION_KEY"):
                validate_key()

    def test_invalid_key_raises(self) -> None:
        """Invalid key format raises EncryptionError."""
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": "not-a-valid-key"}):
            with pytest.raises(EncryptionError, match="Invalid encryption key"):
                validate_key()


class TestDecryptErrors:
    """Tests for decryption error handling."""

    def test_wrong_key_raises(self) -> None:
        """Decrypting with wrong key raises EncryptionError."""
        key1 = Fernet.generate_key().decode()
        key2 = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": key1}):
            ciphertext = encrypt("secret")
        reset_encryption()
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": key2}):
            with pytest.raises(EncryptionError, match="wrong key"):
                decrypt(ciphertext)

    def test_corrupted_ciphertext_raises(self) -> None:
        """Corrupted ciphertext raises EncryptionError."""
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": key}):
            with pytest.raises(EncryptionError, match="wrong key"):
                decrypt("not-valid-ciphertext")
