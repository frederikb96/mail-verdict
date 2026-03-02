"""Tests for IMAP connector: connection, retry, pool, capability detection."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mail_verdict.config.loader import AccountConfig, RetryConfig
from mail_verdict.sync.connector import IMAPConnectionError, IMAPConnector


def _make_account(**overrides: object) -> AccountConfig:
    """Create a test AccountConfig with sensible defaults."""
    defaults = dict(
        name="test",
        host="localhost",
        port=993,
        username="user",
        password="pass",
        folders=["INBOX"],
        idle_folders=["INBOX"],
        ssl_verify=False,
    )
    defaults.update(overrides)
    return AccountConfig(**defaults)  # type: ignore[arg-type]


def _make_retry(**overrides: object) -> RetryConfig:
    """Create a test RetryConfig."""
    defaults = dict(
        max_retries=2,
        base_delay_seconds=0.01,
        max_delay_seconds=0.05,
        exponential_base=2.0,
    )
    defaults.update(overrides)
    return RetryConfig(**defaults)  # type: ignore[arg-type]


class TestIMAPConnector:
    """Tests for IMAPConnector."""

    def test_account_name(self) -> None:
        """account_name returns the account name."""
        connector = IMAPConnector(_make_account(name="mybox"), _make_retry())
        assert connector.account_name == "mybox"

    def test_capability_checks_empty_initially(self) -> None:
        """No capabilities before first connect."""
        connector = IMAPConnector(_make_account(), _make_retry())
        assert connector.has_condstore() is False
        assert connector.has_qresync() is False
        assert connector.has_idle() is False
        assert connector.has_special_use() is False

    @pytest.mark.asyncio
    async def test_connect_with_retry_exhausts(self) -> None:
        """All retries exhausted raises IMAPConnectionError."""
        connector = IMAPConnector(_make_account(), _make_retry(max_retries=1))
        with patch.object(connector, "connect", side_effect=IMAPConnectionError("fail")):
            with pytest.raises(IMAPConnectionError, match="All 2 connection attempts failed"):
                await connector.connect_with_retry()

    @pytest.mark.asyncio
    async def test_connect_with_retry_succeeds_on_second(self) -> None:
        """Succeeds if connect works on second attempt."""
        connector = IMAPConnector(_make_account(), _make_retry(max_retries=2))
        mock_conn = MagicMock()
        with patch.object(
            connector,
            "connect",
            side_effect=[IMAPConnectionError("fail"), mock_conn],
        ):
            result = await connector.connect_with_retry()
            assert result is mock_conn

    def test_is_alive_auth_state(self) -> None:
        """_is_alive returns True for AUTH state."""
        connector = IMAPConnector(_make_account(), _make_retry())
        conn = MagicMock()
        conn.client.get_state.return_value = "AUTH"
        assert connector._is_alive(conn) is True

    def test_is_alive_selected_state(self) -> None:
        """_is_alive returns True for SELECTED state."""
        connector = IMAPConnector(_make_account(), _make_retry())
        conn = MagicMock()
        conn.client.get_state.return_value = "SELECTED"
        assert connector._is_alive(conn) is True

    def test_is_alive_disconnected(self) -> None:
        """_is_alive returns False for other states."""
        connector = IMAPConnector(_make_account(), _make_retry())
        conn = MagicMock()
        conn.client.get_state.return_value = "LOGOUT"
        assert connector._is_alive(conn) is False

    def test_is_alive_exception(self) -> None:
        """_is_alive returns False when get_state raises."""
        connector = IMAPConnector(_make_account(), _make_retry())
        conn = MagicMock()
        conn.client.get_state.side_effect = RuntimeError("dead")
        assert connector._is_alive(conn) is False

    @pytest.mark.asyncio
    async def test_close_sets_closed_flag(self) -> None:
        """close() sets _closed flag."""
        connector = IMAPConnector(_make_account(), _make_retry())
        await connector.close()
        assert connector._closed is True
