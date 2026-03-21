"""Tests for IMAP connector: connection, retry, pool management."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mail_verdict.core.retry import RetryConfig
from mail_verdict.sync.connector import AccountConnConfig, IMAPConnectionError, IMAPConnector


def _make_account(**overrides: object) -> AccountConnConfig:
    """Create a test AccountConnConfig with sensible defaults."""
    defaults: dict[str, Any] = dict(
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
    return AccountConnConfig(**defaults)


def _make_retry(**overrides: Any) -> RetryConfig:
    """Create a test RetryConfig."""
    defaults: dict[str, Any] = dict(
        max_retries=2,
        base_delay_seconds=0.01,
        max_delay_seconds=0.05,
        exponential_base=2.0,
    )
    defaults.update(overrides)
    return RetryConfig.from_settings(defaults)


class TestIMAPConnector:
    """Tests for IMAPConnector."""

    def test_account_name(self) -> None:
        """account_name returns the account name."""
        connector = IMAPConnector(_make_account(name="mybox"), _make_retry())
        assert connector.account_name == "mybox"

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

    @pytest.mark.asyncio
    async def test_is_alive_noop_success(self) -> None:
        """_is_alive returns True when NOOP succeeds."""
        connector = IMAPConnector(_make_account(), _make_retry())
        conn = MagicMock()
        conn.client.noop.return_value = ("OK", [])
        result = await connector._is_alive(conn)
        assert result is True

    @pytest.mark.asyncio
    async def test_is_alive_noop_failure(self) -> None:
        """_is_alive returns False when NOOP raises."""
        connector = IMAPConnector(_make_account(), _make_retry())
        conn = MagicMock()
        conn.client.noop.side_effect = RuntimeError("dead")
        result = await connector._is_alive(conn)
        assert result is False

    @pytest.mark.asyncio
    async def test_close_sets_closed_flag(self) -> None:
        """close() sets _closed flag."""
        connector = IMAPConnector(_make_account(), _make_retry())
        await connector.close()
        assert connector._closed is True
