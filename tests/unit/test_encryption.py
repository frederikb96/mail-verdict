"""Tests for AES-256-GCM provider key encryption."""

from __future__ import annotations

import pytest

from mail_verdict.core.encryption import EncryptionError, decrypt, encrypt

_KEY = "0123456789abcdef" * 4  # 64 hex chars
_OTHER_KEY = "fedcba9876543210" * 4


class TestRoundTrip:
    """Encrypt/decrypt round-trips recover the original plaintext."""

    def test_round_trip(self) -> None:
        assert decrypt(encrypt("sk-ant-secret-value", _KEY), _KEY) == "sk-ant-secret-value"

    def test_two_encryptions_of_the_same_value_differ(self) -> None:
        """A fresh random IV per call means ciphertext is never repeated."""
        a = encrypt("same-plaintext", _KEY)
        b = encrypt("same-plaintext", _KEY)
        assert a != b


class TestWrongKey:
    """Decryption with the wrong key fails loudly, not silently."""

    def test_wrong_key_raises(self) -> None:
        ciphertext = encrypt("secret", _KEY)
        with pytest.raises(EncryptionError, match="decryption failed"):
            decrypt(ciphertext, _OTHER_KEY)


class TestMalformedKey:
    """A key that isn't 64 hex characters is rejected, not silently truncated."""

    def test_short_key_raises(self) -> None:
        with pytest.raises(EncryptionError, match="64 hex characters"):
            encrypt("x", "abc")

    def test_non_hex_key_raises(self) -> None:
        with pytest.raises(EncryptionError, match="not valid hex"):
            encrypt("x", "z" * 64)


class TestMalformedCiphertext:
    """Truncated or corrupted ciphertext is rejected, not silently accepted."""

    def test_too_short_raises(self) -> None:
        with pytest.raises(EncryptionError, match="too short"):
            decrypt(b"short", _KEY)
