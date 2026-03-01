"""
E2E: SMTP forwarding.

Flow: rule forward_to action sends email via SMTP with template variables.
Verifies the ActionPropagator -> SMTPClient -> aiosmtplib chain.

Markers: @pytest.mark.e2e
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from email.message import EmailMessage
from unittest.mock import AsyncMock, MagicMock

import pytest

from mail_verdict.config import AccountConfig, RetryConfig
from mail_verdict.rules.conditions import MailContext
from mail_verdict.rules.executor import ActionExecutor, _render_template
from mail_verdict.sync.actions import ActionPropagator, ForwardAction
from mail_verdict.sync.smtp_client import SMTPClient, SMTPError

TEST_RETRY = RetryConfig(
    max_retries=1,
    base_delay_seconds=0.01,
    max_delay_seconds=0.05,
    exponential_base=2.0,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.asyncio(loop_scope="module"),
]


def _build_raw_message(
    from_addr: str = "original@sender.com",
    to_addr: str = "recipient@example.com",
    subject: str = "Original Subject",
    body: str = "Original body text.",
) -> bytes:
    """Build a raw RFC 2822 email message for forwarding tests."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = "Mon, 01 Jan 2024 12:00:00 +0000"
    msg.set_content(body)
    return msg.as_bytes()


class TestTemplateRendering:
    """Test template variable substitution in forward subjects."""

    def test_basic_template(self) -> None:
        """Template variables are replaced with mail context values."""
        ctx = MailContext(
            from_addr="sender@example.com",
            subject="Monthly Report",
            folder="INBOX",
        )
        result = _render_template("Fwd: {subject} from {from}", ctx)
        assert result == "Fwd: Monthly Report from sender@example.com"

    def test_all_variables(self) -> None:
        """All supported template variables render correctly."""
        ctx = MailContext(
            from_addr="sender@example.com",
            to_addrs=["recipient@example.com"],
            subject="Test Subject",
            folder="INBOX",
            tags=["important", "urgent"],
        )
        result = _render_template(
            "From:{from} To:{to} Subj:{subject} Folder:{folder} Tags:{tags}",
            ctx,
        )
        assert "sender@example.com" in result
        assert "recipient@example.com" in result
        assert "Test Subject" in result
        assert "INBOX" in result
        assert "important" in result

    def test_missing_variables_preserved(self) -> None:
        """Unknown template variables are left as-is."""
        ctx = MailContext(subject="Test")
        result = _render_template("{subject} - {unknown_var}", ctx)
        assert result == "Test - {unknown_var}"

    def test_empty_context(self) -> None:
        """Empty context produces empty substitutions."""
        ctx = MailContext()
        result = _render_template("From: {from}, Subject: {subject}", ctx)
        assert result == "From: , Subject: "


class TestSMTPClientForward:
    """Test SMTPClient forward methods."""

    async def test_forward_attached_mode(self) -> None:
        """
        SMTPClient.forward in 'attached' mode builds a multipart with
        the original as message/rfc822 attachment.
        """
        account = MagicMock(spec=AccountConfig)
        account.username = "user@test.com"

        client = SMTPClient(account, TEST_RETRY)
        # Manually configure SMTP settings
        client._smtp_host = "localhost"
        client._smtp_port = 1025
        client._smtp_user = "user@test.com"
        client._smtp_password = None

        raw_msg = _build_raw_message(
            subject="Important Report",
            from_addr="boss@company.com",
        )

        sent_messages: list[EmailMessage] = []

        async def mock_send(msg: EmailMessage) -> None:
            sent_messages.append(msg)

        client._send_with_retry = mock_send  # type: ignore[assignment]

        await client.forward(
            raw_message=raw_msg,
            to_address="admin@company.com",
            subject_template="Fwd: {subject}",
            mode="attached",
        )

        assert len(sent_messages) == 1
        sent = sent_messages[0]
        assert sent["To"] == "admin@company.com"
        assert "Important Report" in sent["Subject"]

    async def test_forward_inline_mode(self) -> None:
        """
        SMTPClient.forward in 'inline' mode includes original text inline.
        """
        account = MagicMock(spec=AccountConfig)
        account.username = "user@test.com"

        client = SMTPClient(account, TEST_RETRY)
        client._smtp_host = "localhost"
        client._smtp_port = 1025
        client._smtp_user = "user@test.com"
        client._smtp_password = None

        raw_msg = _build_raw_message(
            subject="Hello Friend",
            from_addr="friend@example.com",
            body="How are you doing?",
        )

        sent_messages: list[EmailMessage] = []

        async def mock_send(msg: EmailMessage) -> None:
            sent_messages.append(msg)

        client._send_with_retry = mock_send  # type: ignore[assignment]

        await client.forward(
            raw_message=raw_msg,
            to_address="archive@company.com",
            subject_template="[Archive] {subject}",
            mode="inline",
        )

        assert len(sent_messages) == 1
        sent = sent_messages[0]
        assert "[Archive] Hello Friend" in sent["Subject"]
        body_content = sent.get_content()
        assert "Forwarded message" in body_content
        assert "friend@example.com" in body_content

    async def test_unconfigured_smtp_raises(self) -> None:
        """
        Attempting to forward without SMTP config raises SMTPError.
        """
        account = MagicMock(spec=AccountConfig)
        client = SMTPClient(account, TEST_RETRY)
        client._smtp_host = None

        assert client.configured is False

        with pytest.raises(SMTPError, match="not configured"):
            await client.forward(
                raw_message=b"test",
                to_address="nobody@example.com",
            )

    async def test_template_variables_in_forward_subject(self) -> None:
        """
        Forward subject template resolves {from}, {subject}, {to}, {date}.
        """
        account = MagicMock(spec=AccountConfig)
        account.username = "user@test.com"

        client = SMTPClient(account, TEST_RETRY)
        client._smtp_host = "localhost"
        client._smtp_port = 1025
        client._smtp_user = "user@test.com"
        client._smtp_password = None

        raw_msg = _build_raw_message(
            from_addr="reporter@news.com",
            to_addr="subscriber@example.com",
            subject="Breaking News",
        )

        sent_messages: list[EmailMessage] = []

        async def mock_send(msg: EmailMessage) -> None:
            sent_messages.append(msg)

        client._send_with_retry = mock_send  # type: ignore[assignment]

        await client.forward(
            raw_message=raw_msg,
            to_address="admin@company.com",
            subject_template="[FWD from {from}] {subject}",
        )

        assert len(sent_messages) == 1
        subj = sent_messages[0]["Subject"]
        assert "reporter@news.com" in subj
        assert "Breaking News" in subj


class TestActionPropagatorForward:
    """Test ActionPropagator.execute_forward integration."""

    async def test_execute_forward_success(self) -> None:
        """
        ActionPropagator.execute_forward fetches from IMAP and sends via SMTP.
        """
        mock_connector = MagicMock()
        mock_smtp = AsyncMock(spec=SMTPClient)

        # Mock IMAP connection that returns a raw message on FETCH
        raw_msg = _build_raw_message(subject="Test Forward")
        mock_conn = AsyncMock()
        mock_conn.select_plain.return_value = MagicMock(ok=True)
        mock_conn.client.uid.return_value = MagicMock(
            result="OK",
            lines=[b"header", raw_msg],
        )

        @asynccontextmanager
        async def _acquire():
            yield mock_conn

        mock_connector.acquire = _acquire

        propagator = ActionPropagator(
            connector=mock_connector,
            retry_config=TEST_RETRY,
            smtp_client=mock_smtp,
        )

        action = ForwardAction(
            folder="INBOX",
            uid=100,
            to_address="forward@example.com",
            subject_template="Fwd: {subject}",
        )

        result = await propagator.execute_forward(action)
        assert result is True
        mock_smtp.forward.assert_called_once()

    async def test_execute_forward_no_smtp_client(self) -> None:
        """
        ActionPropagator without SMTP client returns False.
        """
        mock_connector = AsyncMock()
        propagator = ActionPropagator(
            connector=mock_connector,
            retry_config=TEST_RETRY,
            smtp_client=None,
        )

        action = ForwardAction(
            folder="INBOX",
            uid=100,
            to_address="forward@example.com",
        )

        result = await propagator.execute_forward(action)
        assert result is False


class TestRuleForwardToAction:
    """Test forward_to action in the rule executor."""

    async def test_forward_to_simple_address(self) -> None:
        """
        forward_to with a plain address string.
        """
        mock_propagator = AsyncMock()
        mock_propagator.execute_forward.return_value = True

        executor = ActionExecutor(propagator=mock_propagator)
        ctx = MailContext(
            folder="INBOX",
            subject="Alert Email",
            from_addr="monitoring@server.com",
        )

        result = await executor.execute(
            {"forward_to": "admin@company.com"},
            ctx,
            uid=42,
        )
        assert result.success is True
        assert result.action_type == "forward_to"

        call_args = mock_propagator.execute_forward.call_args
        fwd = call_args.args[0]
        assert fwd.to_address == "admin@company.com"
        assert fwd.folder == "INBOX"
        assert fwd.uid == 42

    async def test_forward_to_with_subject_rewrite(self) -> None:
        """
        forward_to with dict containing address and subject_rewrite template.
        """
        mock_propagator = AsyncMock()
        mock_propagator.execute_forward.return_value = True

        executor = ActionExecutor(propagator=mock_propagator)
        ctx = MailContext(
            folder="INBOX",
            subject="Server Alert",
            from_addr="alerts@monitoring.com",
            tags=["critical"],
        )

        result = await executor.execute(
            {
                "forward_to": {
                    "address": "oncall@company.com",
                    "subject_rewrite": "[ALERT] {subject} - tags: {tags}",
                }
            },
            ctx,
            uid=99,
        )
        assert result.success is True

        call_args = mock_propagator.execute_forward.call_args
        fwd = call_args.args[0]
        assert fwd.to_address == "oncall@company.com"
        assert "[ALERT] Server Alert" in fwd.subject_template
        assert "critical" in fwd.subject_template

    async def test_forward_to_no_propagator(self) -> None:
        """
        forward_to without ActionPropagator logs warning but succeeds.
        """
        executor = ActionExecutor(propagator=None)
        ctx = MailContext(folder="INBOX", subject="Test")

        result = await executor.execute(
            {"forward_to": "admin@company.com"},
            ctx,
            uid=1,
        )
        # Succeeds but does nothing (warning logged)
        assert result.success is True


class TestSMTPRetryBehavior:
    """Test SMTP retry and error handling."""

    async def test_send_with_retry_exhausted(self) -> None:
        """
        After exhausting retries, SMTPError is raised.
        """
        account = MagicMock(spec=AccountConfig)
        account.username = "user@test.com"

        client = SMTPClient(account, TEST_RETRY)
        client._smtp_host = "192.0.2.1"  # RFC 5737 unreachable
        client._smtp_port = 465
        client._smtp_user = "user"
        client._smtp_password = "pass"

        msg = EmailMessage()
        msg["From"] = "user@test.com"
        msg["To"] = "recipient@example.com"
        msg["Subject"] = "Test"
        msg.set_content("Test body")

        with pytest.raises(SMTPError, match="Failed to send"):
            await client._send_with_retry(msg)

    async def test_send_simple_email(self) -> None:
        """
        SMTPClient.send builds a simple email and sends it.
        """
        account = MagicMock(spec=AccountConfig)
        account.username = "user@test.com"

        client = SMTPClient(account, TEST_RETRY)
        client._smtp_host = "localhost"
        client._smtp_port = 1025
        client._smtp_user = "user@test.com"
        client._smtp_password = None

        sent_messages: list[EmailMessage] = []

        async def mock_send(msg: EmailMessage) -> None:
            sent_messages.append(msg)

        client._send_with_retry = mock_send  # type: ignore[assignment]

        await client.send(
            to_address="recipient@example.com",
            subject="Test Send",
            body="Hello World",
        )

        assert len(sent_messages) == 1
        assert sent_messages[0]["Subject"] == "Test Send"
        assert sent_messages[0]["To"] == "recipient@example.com"
