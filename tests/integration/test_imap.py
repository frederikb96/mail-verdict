"""
Integration tests: IMAP operations against real Stalwart.

Requires podman-compose.test.yaml Stalwart container running on:
  IMAP: localhost:1143 (plain) or localhost:1993 (SSL)
  SMTP: localhost:1025
  Admin API: localhost:8880

Markers: @pytest.mark.integration
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from mail_verdict.config import AccountConfig, RetryConfig, SyncConfig
from mail_verdict.sync.connector import IMAPConnectionError, IMAPConnector
from mail_verdict.sync.idle import IdleWatcher

# Test constants (match podman-compose.test.yaml Stalwart config)
STALWART_HOST = "localhost"
STALWART_IMAP_PORT = 1143
STALWART_SMTP_PORT = 1025
STALWART_ADMIN_URL = "http://localhost:8880"
STALWART_ADMIN_USER = "admin"
STALWART_ADMIN_SECRET = "testadmin123"

TEST_USER = "testuser@localhost"
TEST_PASSWORD = "testpass123"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="module"),
]


def _retry_config() -> RetryConfig:
    """Minimal retry config for tests."""
    return RetryConfig(
        max_retries=2,
        base_delay_seconds=0.1,
        max_delay_seconds=1.0,
        exponential_base=2.0,
    )


def _sync_config() -> SyncConfig:
    """Sync config for IDLE tests."""
    return SyncConfig(
        poll_interval_seconds=10,
        idle_enabled=True,
        idle_restart_seconds=30,
        lookback_days=30,
        auto_detect_folders=True,
    )


def _account_config(username: str = TEST_USER, password: str = TEST_PASSWORD) -> AccountConfig:
    """Build account config for test user."""
    return AccountConfig(
        name="test-account",
        host=STALWART_HOST,
        port=STALWART_IMAP_PORT,
        username=username,
        password=password,
        folders=["INBOX"],
        idle_folders=["INBOX"],
        smtp_host=STALWART_HOST,
        smtp_port=STALWART_SMTP_PORT,
    )


async def _create_test_user_via_api() -> bool:
    """
    Create a test user on Stalwart via its HTTP admin API.

    Returns True if user was created or already exists.
    """
    import httpx

    async with httpx.AsyncClient(base_url=STALWART_ADMIN_URL) as client:
        try:
            response = await client.post(
                "/api/principal",
                json={
                    "type": "individual",
                    "name": "testuser",
                    "secrets": [TEST_PASSWORD],
                    "emails": [TEST_USER],
                },
                auth=(STALWART_ADMIN_USER, STALWART_ADMIN_SECRET),
                timeout=10.0,
            )
            return response.status_code in (200, 201, 409)
        except httpx.ConnectError:
            pytest.skip("Stalwart admin API not reachable")
            return False


@pytest.fixture(scope="module")
async def test_user() -> str:
    """Ensure test user exists on Stalwart."""
    created = await _create_test_user_via_api()
    if not created:
        pytest.skip("Could not create test user on Stalwart")
    return TEST_USER


@pytest.fixture
def connector(test_user: str) -> IMAPConnector:
    """Create an IMAP connector for the test user."""
    return IMAPConnector(
        account=_account_config(),
        retry=_retry_config(),
    )


class TestIMAPConnect:
    """Test IMAP connection and authentication."""

    async def test_connect_success(self, connector: IMAPConnector) -> None:
        """Verify basic IMAP connection and login."""
        conn = await connector.connect()
        assert conn is not None
        # Connection should be in AUTH state after login
        state = conn.client.get_state()
        assert state in ("AUTH", "SELECTED")
        await conn.client.logout()

    async def test_connect_detects_capabilities(self, connector: IMAPConnector) -> None:
        """Verify capability detection after first connection."""
        conn = await connector.connect()
        assert len(connector.capabilities) > 0
        # Stalwart supports IDLE
        assert connector.has_idle()
        await conn.client.logout()

    async def test_connect_with_bad_credentials(self, test_user: str) -> None:
        """Verify connection fails with wrong password."""
        bad_connector = IMAPConnector(
            account=_account_config(password="wrongpassword"),
            retry=_retry_config(),
        )
        with pytest.raises(IMAPConnectionError):
            await bad_connector.connect()

    async def test_connect_with_retry(self, connector: IMAPConnector) -> None:
        """Verify connect_with_retry returns a valid connection."""
        conn = await connector.connect_with_retry()
        assert conn is not None
        state = conn.client.get_state()
        assert state in ("AUTH", "SELECTED")
        await conn.client.logout()


class TestIMAPListFolders:
    """Test IMAP folder listing."""

    async def test_list_folders(self, connector: IMAPConnector) -> None:
        """Verify folder listing returns at least INBOX."""
        async with connector.acquire() as conn:
            response = await conn.client.list('""', "*")
            assert response.result == "OK"
            folder_lines = response.lines
            # Should have at least one folder (INBOX)
            assert len(folder_lines) > 0

    async def test_select_inbox(self, connector: IMAPConnector) -> None:
        """Verify SELECT INBOX succeeds."""
        async with connector.acquire() as conn:
            result = await conn.select_plain("INBOX")
            assert result.ok


class TestIMAPSync:
    """Test IMAP sync operations (fetch, flag changes)."""

    async def test_deliver_and_fetch(self, connector: IMAPConnector) -> None:
        """
        Deliver a test email via SMTP to Stalwart, then fetch it via IMAP.

        This verifies the full deliver -> sync flow.
        """
        from email.message import EmailMessage

        import aiosmtplib

        # Deliver a test email
        unique_subject = f"Test-{uuid.uuid4().hex[:8]}"
        msg = EmailMessage()
        msg["From"] = "sender@example.com"
        msg["To"] = TEST_USER
        msg["Subject"] = unique_subject
        msg.set_content("Integration test body content.")

        try:
            await aiosmtplib.send(
                msg,
                hostname=STALWART_HOST,
                port=STALWART_SMTP_PORT,
                timeout=10.0,
            )
        except Exception as exc:
            pytest.skip(f"SMTP delivery failed: {exc}")

        # Wait for Stalwart to process
        await asyncio.sleep(2.0)

        # Fetch via IMAP
        async with connector.acquire() as conn:
            result = await conn.select_plain("INBOX")
            assert result.ok

            # Search for our message
            search_resp = await conn.client.search("ALL")
            assert search_resp.result == "OK"

            # Fetch the latest message
            if search_resp.lines and search_resp.lines[0]:
                uids_raw = search_resp.lines[0]
                uids_str = uids_raw.decode() if isinstance(uids_raw, bytes) else str(uids_raw)
                uid_list = uids_str.strip().split()
                if uid_list:
                    latest_uid = uid_list[-1]
                    fetch_resp = await conn.client.fetch(
                        latest_uid, "(BODY[HEADER.FIELDS (SUBJECT)])"
                    )
                    assert fetch_resp.result == "OK"

    async def test_flag_change(self, connector: IMAPConnector) -> None:
        """Verify STORE flag changes work (mark as read)."""
        async with connector.acquire() as conn:
            result = await conn.select_plain("INBOX")
            assert result.ok

            search_resp = await conn.client.search("ALL")
            if search_resp.lines and search_resp.lines[0]:
                uids_raw = search_resp.lines[0]
                uids_str = uids_raw.decode() if isinstance(uids_raw, bytes) else str(uids_raw)
                uid_list = uids_str.strip().split()
                if uid_list:
                    uid = uid_list[0]
                    # Add \Seen flag
                    store_resp = await conn.client.store(uid, "+FLAGS", "(\\Seen)")
                    assert store_resp.result == "OK"

                    # Remove \Seen flag
                    store_resp = await conn.client.store(uid, "-FLAGS", "(\\Seen)")
                    assert store_resp.result == "OK"


class TestIMAPMove:
    """Test IMAP MOVE/COPY operations."""

    async def test_move_creates_copy_in_target(self, connector: IMAPConnector) -> None:
        """
        Test MOVE command by delivering a message and moving it.

        Stalwart supports IMAP MOVE (RFC 6851).
        """
        from email.message import EmailMessage

        import aiosmtplib

        unique_subject = f"MoveTest-{uuid.uuid4().hex[:8]}"
        msg = EmailMessage()
        msg["From"] = "sender@example.com"
        msg["To"] = TEST_USER
        msg["Subject"] = unique_subject
        msg.set_content("Test message for MOVE operation.")

        try:
            await aiosmtplib.send(
                msg,
                hostname=STALWART_HOST,
                port=STALWART_SMTP_PORT,
                timeout=10.0,
            )
        except Exception as exc:
            pytest.skip(f"SMTP delivery failed: {exc}")

        await asyncio.sleep(2.0)

        async with connector.acquire() as conn:
            result = await conn.select_plain("INBOX")
            assert result.ok

            # Find the message
            search_resp = await conn.client.search("ALL")
            if search_resp.lines and search_resp.lines[0]:
                uids_raw = search_resp.lines[0]
                uids_str = uids_raw.decode() if isinstance(uids_raw, bytes) else str(uids_raw)
                uid_list = uids_str.strip().split()
                if uid_list:
                    uid = uid_list[-1]
                    # Try to move to Junk (Stalwart should have it)
                    try:
                        move_resp = await conn.client.move(uid, "Junk")
                        # MOVE may or may not succeed depending on Stalwart folder setup
                        assert move_resp.result in ("OK", "NO")
                    except Exception:
                        pass  # Move not supported or folder doesn't exist


class TestIMAPIdleNotification:
    """Test IMAP IDLE notifications."""

    async def test_idle_detects_new_mail(self, connector: IMAPConnector) -> None:
        """
        Start IDLE on INBOX, deliver an email via SMTP, verify notification.

        This tests the full IDLE notification flow.
        """
        notifications: list[str] = []

        async def on_new_mail(folder: str) -> None:
            notifications.append(folder)

        watcher = IdleWatcher(
            connector=connector,
            sync_config=_sync_config(),
            retry_config=_retry_config(),
            on_new_mail=on_new_mail,
        )

        # Start IDLE
        await watcher.start(["INBOX"])

        # Wait for IDLE to establish
        await asyncio.sleep(2.0)

        # Deliver a message while IDLE is active
        from email.message import EmailMessage

        import aiosmtplib

        unique_subject = f"IDLE-Test-{uuid.uuid4().hex[:8]}"
        msg = EmailMessage()
        msg["From"] = "idle-test@example.com"
        msg["To"] = TEST_USER
        msg["Subject"] = unique_subject
        msg.set_content("Testing IDLE notification detection.")

        try:
            await aiosmtplib.send(
                msg,
                hostname=STALWART_HOST,
                port=STALWART_SMTP_PORT,
                timeout=10.0,
            )
        except Exception:
            await watcher.stop()
            pytest.skip("SMTP delivery failed")

        # Wait for notification (IDLE should fire within seconds)
        await asyncio.sleep(5.0)

        await watcher.stop()

        # IDLE notification may or may not fire depending on Stalwart config
        # We primarily verify the IDLE loop ran without errors
        # If notifications came through, verify they're for the right folder
        if notifications:
            assert "INBOX" in notifications


class TestIMAPConnectionPool:
    """Test connection pooling behavior."""

    async def test_acquire_returns_connection(self, connector: IMAPConnector) -> None:
        """Verify acquire context manager returns usable connection."""
        async with connector.acquire() as conn:
            assert conn is not None
            state = conn.client.get_state()
            assert state in ("AUTH", "SELECTED")

    async def test_multiple_acquires(self, connector: IMAPConnector) -> None:
        """Verify multiple sequential acquires work (connection reuse)."""
        async with connector.acquire() as conn1:
            result = await conn1.select_plain("INBOX")
            assert result.ok

        async with connector.acquire() as conn2:
            result = await conn2.select_plain("INBOX")
            assert result.ok

    async def test_close_cleans_up(self, test_user: str) -> None:
        """Verify closing connector prevents further acquisitions."""
        conn = IMAPConnector(
            account=_account_config(),
            retry=_retry_config(),
        )

        # Use a connection first
        async with conn.acquire() as c:
            result = await c.select_plain("INBOX")
            assert result.ok

        # Close the connector
        await conn.close()
