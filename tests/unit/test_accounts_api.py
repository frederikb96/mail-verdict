"""Tests for Account API: CRUD, encryption, PostIMAP account model."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from mail_verdict.core.encryption import decrypt, encrypt, reset_encryption

TEST_HEX_KEY = "0" * 64


@pytest.fixture(autouse=True)
def _setup_encryption_key() -> None:
    """Provide a test encryption key and reset state."""
    reset_encryption()
    with patch.dict(os.environ, {"MAIL_VERDICT_ENCRYPTION_KEY": TEST_HEX_KEY}):
        yield
    reset_encryption()


class TestAccountEncryption:
    """Tests for password encryption in account operations."""

    def test_encrypt_produces_non_plaintext(self) -> None:
        """Encrypted password is bytes, round-trips correctly."""
        ciphertext = encrypt("my_imap_password")
        assert isinstance(ciphertext, bytes)
        assert ciphertext != b"my_imap_password"
        assert len(ciphertext) > 20
        assert decrypt(ciphertext) == "my_imap_password"

    def test_account_create_request_schema(self) -> None:
        """AccountCreateRequest includes password and prefs fields."""
        from mail_verdict.api.schemas import AccountCreateRequest

        req = AccountCreateRequest(
            name="test",
            imap_host="imap.example.com",
            imap_port=993,
            imap_user="user@example.com",
            imap_password="secret",
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_user="user@example.com",
            smtp_password="smtp_secret",
            spam_enabled=True,
            embedding_lookback_days=90,
        )
        assert req.imap_password == "secret"
        assert req.smtp_password == "smtp_secret"
        assert req.spam_enabled is True
        assert req.embedding_lookback_days == 90


class TestAccountResponseSchema:
    """Tests for AccountResponse schema."""

    def test_response_excludes_passwords(self) -> None:
        """AccountResponse does NOT have password fields."""
        from mail_verdict.api.schemas import AccountResponse

        fields = set(AccountResponse.model_fields.keys())
        assert "imap_password" not in fields
        assert "smtp_password" not in fields

    def test_response_includes_postimap_fields(self) -> None:
        """AccountResponse includes state and prefs fields."""
        from mail_verdict.api.schemas import AccountResponse

        fields = set(AccountResponse.model_fields.keys())
        assert "state" in fields
        assert "spam_enabled" in fields
        assert "embedding_lookback_days" in fields


class TestAccountUpdateSchema:
    """Tests for AccountUpdateRequest schema."""

    def test_update_request_has_password_fields(self) -> None:
        """AccountUpdateRequest allows password updates."""
        from mail_verdict.api.schemas import AccountUpdateRequest

        req = AccountUpdateRequest(imap_password="new_secret")
        dump = req.model_dump(exclude_unset=True)
        assert "imap_password" in dump
        assert dump["imap_password"] == "new_secret"

    def test_update_request_partial(self) -> None:
        """Only specified fields appear in dump."""
        from mail_verdict.api.schemas import AccountUpdateRequest

        req = AccountUpdateRequest(spam_enabled=True)
        dump = req.model_dump(exclude_unset=True)
        assert "spam_enabled" in dump
        assert "name" not in dump


class TestAccountModel:
    """Tests for Account database model fields (PostIMAP-owned)."""

    def test_account_model_has_core_columns(self) -> None:
        """Account model includes encrypted password and state fields."""
        from mail_verdict.database.models import Account

        assert hasattr(Account, "imap_password")
        assert hasattr(Account, "smtp_password")
        assert hasattr(Account, "state")
        assert hasattr(Account, "capabilities")

    def test_account_prefs_model_has_pref_columns(self) -> None:
        """AccountPrefs model has spam_enabled and embedding_lookback_days."""
        from mail_verdict.database.models import AccountPrefs

        assert hasattr(AccountPrefs, "spam_enabled")
        assert hasattr(AccountPrefs, "embedding_lookback_days")
        assert hasattr(AccountPrefs, "folder_mapping")

    def test_account_state_enum_values(self) -> None:
        """AccountState enum has all expected values."""
        from mail_verdict.database.models import AccountState

        states = {s.value for s in AccountState}
        assert states == {"created", "syncing", "active", "error", "disabled"}
