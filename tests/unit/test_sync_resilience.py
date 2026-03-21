"""Tests for IMAP sync resilience: reconnection, reconciliation, atomic batches."""

from __future__ import annotations

import asyncio
import imaplib
import socket
import ssl
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mail_verdict.core.retry import RetryConfig
from mail_verdict.sync.actions import (
    ActionPropagator,
    ActionType,
    BatchResult,
    IMAPAction,
)
from mail_verdict.sync.connector import (
    AccountConnConfig,
    IMAPConnectionError,
    IMAPConnector,
    is_connection_error,
)


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
    """Create a test RetryConfig with fast delays for testing."""
    defaults: dict[str, Any] = dict(
        max_retries=2,
        base_delay_seconds=0.001,
        max_delay_seconds=0.01,
        exponential_base=2.0,
    )
    defaults.update(overrides)
    return RetryConfig.from_settings(defaults)


class TestIsConnectionError:
    """Tests for is_connection_error helper."""

    def test_imap_connection_error(self) -> None:
        """IMAPConnectionError is classified as connection error."""
        assert is_connection_error(IMAPConnectionError("fail"))

    def test_connection_error(self) -> None:
        """stdlib ConnectionError is classified as connection error."""
        assert is_connection_error(ConnectionError("reset"))

    def test_connection_reset_error(self) -> None:
        """ConnectionResetError is classified as connection error."""
        assert is_connection_error(ConnectionResetError("reset"))

    def test_broken_pipe_error(self) -> None:
        """BrokenPipeError is classified as connection error."""
        assert is_connection_error(BrokenPipeError("broken"))

    def test_socket_timeout(self) -> None:
        """socket.timeout is classified as connection error."""
        assert is_connection_error(socket.timeout("timed out"))

    def test_socket_gaierror(self) -> None:
        """socket.gaierror is classified as connection error."""
        assert is_connection_error(socket.gaierror("dns failed"))

    def test_imaplib_error(self) -> None:
        """imaplib.IMAP4.error is classified as connection error."""
        assert is_connection_error(imaplib.IMAP4.error("imap error"))

    def test_imaplib_abort(self) -> None:
        """imaplib.IMAP4.abort is classified as connection error."""
        assert is_connection_error(imaplib.IMAP4.abort("abort"))

    def test_ssl_error(self) -> None:
        """ssl.SSLError is classified as connection error."""
        assert is_connection_error(ssl.SSLError("ssl fail"))

    def test_value_error_not_connection(self) -> None:
        """ValueError is NOT a connection error."""
        assert not is_connection_error(ValueError("bad value"))

    def test_runtime_error_not_connection(self) -> None:
        """RuntimeError is NOT a connection error."""
        assert not is_connection_error(RuntimeError("runtime"))

    def test_keyboard_interrupt_not_connection(self) -> None:
        """KeyboardInterrupt is NOT a connection error."""
        assert not is_connection_error(KeyboardInterrupt())


class TestConnectorDrainPool:
    """Tests for IMAPConnector.drain_pool()."""

    @pytest.mark.asyncio
    async def test_drain_empty_pool(self) -> None:
        """Draining an empty pool is a no-op."""
        connector = IMAPConnector(_make_account(), _make_retry())
        await connector.drain_pool()
        assert connector._pool.empty()

    @pytest.mark.asyncio
    async def test_drain_closes_connections(self) -> None:
        """Draining closes all pooled connections."""
        connector = IMAPConnector(_make_account(), _make_retry())

        mock_conn1 = MagicMock()
        mock_conn1.logout = MagicMock()
        mock_conn2 = MagicMock()
        mock_conn2.logout = MagicMock()

        connector._pool.put_nowait(mock_conn1)
        connector._pool.put_nowait(mock_conn2)
        connector._pool_size = 2

        await connector.drain_pool()

        assert connector._pool.empty()
        assert connector._pool_size == 0

    @pytest.mark.asyncio
    async def test_drain_allows_new_connections(self) -> None:
        """After drain, acquire() creates a fresh connection."""
        connector = IMAPConnector(_make_account(), _make_retry())

        stale_conn = MagicMock()
        stale_conn.logout = MagicMock()
        connector._pool.put_nowait(stale_conn)
        connector._pool_size = 1

        await connector.drain_pool()

        fresh_conn = MagicMock()
        with patch.object(connector, "connect_with_retry", return_value=fresh_conn):
            async with connector.acquire() as conn:
                assert conn is fresh_conn


class TestSyncLoopReconnection:
    """Tests for SyncManager._sync_loop connection loss handling."""

    def _make_manager(
        self,
        sync_settings: dict[str, Any] | None = None,
    ) -> Any:
        """Create a SyncManager with mock dependencies."""
        from mail_verdict.sync.manager import SyncManager

        account = MagicMock()
        account.name = "test-reconnect"

        settings = sync_settings or {
            "poll_interval_seconds": 1,
            "auto_detect_folders": False,
            "base_delay_seconds": 0.001,
            "max_delay_seconds": 0.01,
            "exponential_base": 2.0,
        }

        connector = MagicMock()
        connector.drain_pool = AsyncMock()

        manager = SyncManager(
            account=account,
            account_id=MagicMock(),
            connector=connector,
            folder_repo=MagicMock(),
            mail_repo=MagicMock(),
            attachment_repo=MagicMock(),
            sync_settings=settings,
        )
        return manager

    @pytest.mark.asyncio
    async def test_connection_error_sets_reconciliation_flag(self) -> None:
        """Connection error in sync_once sets _needs_reconciliation."""
        manager = self._make_manager()

        call_count = 0

        async def fake_sync_once() -> list[Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("connection lost")
            # Stop after second call
            manager._running = False
            return []

        with patch.object(manager, "sync_once", side_effect=fake_sync_once):
            manager._running = True
            # Run the loop in a task with a timeout
            task = asyncio.create_task(manager._sync_loop())
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.TimeoutError:
                manager._running = False
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        assert manager._needs_reconciliation is True

    @pytest.mark.asyncio
    async def test_connection_error_drains_pool(self) -> None:
        """Connection error triggers pool drain."""
        manager = self._make_manager()
        manager._connector.drain_pool = AsyncMock()

        call_count = 0

        async def fake_sync_once() -> list[Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionResetError("reset")
            manager._running = False
            return []

        with patch.object(manager, "sync_once", side_effect=fake_sync_once):
            manager._running = True
            task = asyncio.create_task(manager._sync_loop())
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.TimeoutError:
                manager._running = False
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        manager._connector.drain_pool.assert_awaited()

    @pytest.mark.asyncio
    async def test_successful_sync_resets_backoff(self) -> None:
        """Successful sync after connection error resets reconciliation flag."""
        manager = self._make_manager()
        manager._connector.drain_pool = AsyncMock()

        call_count = 0

        async def fake_sync_once() -> list[Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("lost")
            if call_count == 2:
                # Successful sync
                manager._needs_reconciliation = False
                manager._running = False
                return []
            manager._running = False
            return []

        with patch.object(manager, "sync_once", side_effect=fake_sync_once):
            manager._running = True
            task = asyncio.create_task(manager._sync_loop())
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.TimeoutError:
                manager._running = False
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_non_connection_error_no_backoff(self) -> None:
        """Non-connection errors don't set reconciliation flag."""
        manager = self._make_manager()

        call_count = 0

        async def fake_sync_once() -> list[Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("not a connection error")
            manager._running = False
            return []

        with patch.object(manager, "sync_once", side_effect=fake_sync_once):
            manager._running = True
            task = asyncio.create_task(manager._sync_loop())
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.TimeoutError:
                manager._running = False
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        assert manager._needs_reconciliation is False


class TestBatchResult:
    """Tests for BatchResult dataclass."""

    def test_all_succeeded_empty(self) -> None:
        """Empty batch counts as all succeeded."""
        result = BatchResult()
        assert result.all_succeeded is True
        assert result.total == 0

    def test_all_succeeded_true(self) -> None:
        """All succeeded when no failures."""
        result = BatchResult(succeeded_uids=["1", "2", "3"])
        assert result.all_succeeded is True
        assert result.total == 3

    def test_all_succeeded_false(self) -> None:
        """Not all succeeded when failures exist."""
        result = BatchResult(
            succeeded_uids=["1", "2"],
            failed_uids=["3"],
        )
        assert result.all_succeeded is False
        assert result.total == 3


class TestActionPropagatorBatch:
    """Tests for ActionPropagator.execute_batch."""

    def _make_propagator(
        self,
        execute_results: dict[str, bool] | None = None,
    ) -> ActionPropagator:
        """Create an ActionPropagator with a mock connector."""
        connector = MagicMock()
        connector.acquire = MagicMock()
        retry = _make_retry()
        propagator = ActionPropagator(
            connector=connector,
            retry_config=retry,
        )
        return propagator

    @pytest.mark.asyncio
    async def test_batch_all_succeed(self) -> None:
        """All UIDs succeed in batch."""
        propagator = self._make_propagator()

        with patch.object(propagator, "execute_imap", return_value=True):
            result = await propagator.execute_batch(
                IMAPAction(
                    action_type=ActionType.STORE_FLAGS,
                    folder="INBOX",
                    uid_set="1,2,3",
                    flags_add=["\\Seen"],
                )
            )

        assert result.all_succeeded
        assert result.succeeded_uids == ["1", "2", "3"]
        assert result.failed_uids == []

    @pytest.mark.asyncio
    async def test_batch_partial_failure(self) -> None:
        """Some UIDs fail in batch, tracked individually."""
        propagator = self._make_propagator()

        call_count = 0

        async def fake_execute(action: IMAPAction) -> bool:
            nonlocal call_count
            call_count += 1
            # UID "2" fails
            return action.uid_set != "2"

        with patch.object(propagator, "execute_imap", side_effect=fake_execute):
            result = await propagator.execute_batch(
                IMAPAction(
                    action_type=ActionType.STORE_FLAGS,
                    folder="INBOX",
                    uid_set="1,2,3",
                    flags_add=["\\Seen"],
                )
            )

        assert not result.all_succeeded
        assert result.succeeded_uids == ["1", "3"]
        assert result.failed_uids == ["2"]
        assert len(result.errors) == 1

    @pytest.mark.asyncio
    async def test_batch_exception_on_uid(self) -> None:
        """Exception on individual UID is caught and tracked."""
        propagator = self._make_propagator()

        async def fake_execute(action: IMAPAction) -> bool:
            if action.uid_set == "2":
                raise ConnectionError("lost connection")
            return True

        with patch.object(propagator, "execute_imap", side_effect=fake_execute):
            result = await propagator.execute_batch(
                IMAPAction(
                    action_type=ActionType.MOVE,
                    folder="INBOX",
                    uid_set="1,2,3",
                    target_folder="Archive",
                )
            )

        assert result.succeeded_uids == ["1", "3"]
        assert result.failed_uids == ["2"]
        assert any("lost connection" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_batch_empty_uid_set(self) -> None:
        """Empty uid_set returns empty BatchResult."""
        propagator = self._make_propagator()

        result = await propagator.execute_batch(
            IMAPAction(
                action_type=ActionType.STORE_FLAGS,
                folder="INBOX",
                uid_set="",
            )
        )

        assert result.all_succeeded
        assert result.total == 0

    @pytest.mark.asyncio
    async def test_batch_all_fail(self) -> None:
        """All UIDs failing in batch."""
        propagator = self._make_propagator()

        with patch.object(propagator, "execute_imap", return_value=False):
            result = await propagator.execute_batch(
                IMAPAction(
                    action_type=ActionType.STORE_FLAGS,
                    folder="INBOX",
                    uid_set="10,20",
                    flags_add=["\\Flagged"],
                )
            )

        assert not result.all_succeeded
        assert result.succeeded_uids == []
        assert result.failed_uids == ["10", "20"]

    @pytest.mark.asyncio
    async def test_batch_single_uid(self) -> None:
        """Single UID batch works correctly."""
        propagator = self._make_propagator()

        with patch.object(propagator, "execute_imap", return_value=True):
            result = await propagator.execute_batch(
                IMAPAction(
                    action_type=ActionType.MOVE,
                    folder="INBOX",
                    uid_set="42",
                    target_folder="Trash",
                )
            )

        assert result.all_succeeded
        assert result.succeeded_uids == ["42"]

    @pytest.mark.asyncio
    async def test_batch_creates_per_uid_actions(self) -> None:
        """Each UID in batch gets its own execute_imap call."""
        propagator = self._make_propagator()
        captured_actions: list[IMAPAction] = []

        async def capture_execute(action: IMAPAction) -> bool:
            captured_actions.append(action)
            return True

        with patch.object(propagator, "execute_imap", side_effect=capture_execute):
            await propagator.execute_batch(
                IMAPAction(
                    action_type=ActionType.STORE_FLAGS,
                    folder="INBOX",
                    uid_set="1,2,3",
                    flags_add=["\\Seen"],
                    flags_remove=["\\Draft"],
                )
            )

        assert len(captured_actions) == 3
        for i, action in enumerate(captured_actions):
            assert action.uid_set == str(i + 1)
            assert action.folder == "INBOX"
            assert action.flags_add == ["\\Seen"]
            assert action.flags_remove == ["\\Draft"]
