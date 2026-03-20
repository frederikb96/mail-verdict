"""
Async SMTP client for email forwarding.

Uses aiosmtplib for TLS/STARTTLS connections with retry and
template variable substitution in forwarded subjects.
"""

from __future__ import annotations

import asyncio
import email
import email.mime.message
import email.mime.multipart
import email.mime.text
import email.policy
import email.utils
import logging
from email.message import EmailMessage
from typing import TYPE_CHECKING

import aiosmtplib

if TYPE_CHECKING:
    from mail_verdict.core.retry import RetryConfig
    from mail_verdict.sync.connector import AccountConnConfig

logger = logging.getLogger(__name__)


class SMTPError(Exception):
    """Raised when SMTP operations fail."""


class SMTPClient:
    """
    Async SMTP client for sending and forwarding emails.

    Supports TLS (implicit) and STARTTLS (explicit) connections.
    Template variables in subjects: {from}, {to}, {subject}, {date}, {folder}, {tags}.
    """

    def __init__(
        self,
        account: AccountConnConfig,
        retry_config: RetryConfig,
    ) -> None:
        """
        Initialize SMTP client for an account.

        Args:
            account: Account connection config with SMTP settings
            retry_config: Retry configuration
        """
        self._account = account
        self._retry = retry_config
        self._smtp_host: str | None = getattr(account, "smtp_host", None)
        self._smtp_port: int = getattr(account, "smtp_port", 465)
        self._smtp_user: str | None = getattr(account, "smtp_user", None)
        self._smtp_password: str | None = getattr(account, "smtp_password", None)

    @property
    def configured(self) -> bool:
        """Check if SMTP is configured for this account."""
        return self._smtp_host is not None

    async def forward(
        self,
        raw_message: bytes,
        to_address: str,
        subject_template: str = "Fwd: {subject}",
        mode: str = "attached",
    ) -> None:
        """
        Forward an email message.

        Args:
            raw_message: Raw RFC 2822 message bytes
            to_address: Recipient address
            subject_template: Subject with template variables
            mode: "attached" (message/rfc822) or "inline" (text body)

        Raises:
            SMTPError: If sending fails after retries
        """
        if not self.configured:
            raise SMTPError("SMTP not configured for this account")

        original = email.message_from_bytes(raw_message, policy=email.policy.default)

        # Build template context
        context = {
            "from": str(original.get("From", "")),
            "to": str(original.get("To", "")),
            "subject": str(original.get("Subject", "")),
            "date": str(original.get("Date", "")),
            "folder": "",
            "tags": "",
        }

        subject = subject_template.format_map(_SafeFormatDict(context))

        if mode == "attached":
            msg = self._build_attached_forward(original, subject, to_address)
        else:
            msg = self._build_inline_forward(original, subject, to_address)

        await self._send_with_retry(msg)

    async def send(
        self,
        to_address: str,
        subject: str,
        body: str,
        *,
        html: bool = False,
    ) -> None:
        """
        Send a simple email.

        Args:
            to_address: Recipient
            subject: Subject line
            body: Message body
            html: If True, send as HTML

        Raises:
            SMTPError: If sending fails after retries
        """
        if not self.configured:
            raise SMTPError("SMTP not configured for this account")

        msg = EmailMessage()
        msg["From"] = self._smtp_user or self._account.username
        msg["To"] = to_address
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=True)

        if html:
            msg.set_content(body, subtype="html")
        else:
            msg.set_content(body)

        await self._send_with_retry(msg)

    async def _send_with_retry(self, msg: EmailMessage) -> None:
        """
        Send a message with exponential backoff retry.

        Args:
            msg: Email message to send

        Raises:
            SMTPError: If all retries exhausted
        """
        last_error: Exception | None = None

        for attempt in range(self._retry.max_retries + 1):
            try:
                use_tls = self._smtp_port == 465
                smtp = aiosmtplib.SMTP(
                    hostname=self._smtp_host,
                    port=self._smtp_port,
                    use_tls=use_tls,
                )

                await smtp.connect()

                if not use_tls:
                    await smtp.starttls()

                if self._smtp_user and self._smtp_password:
                    await smtp.login(self._smtp_user, self._smtp_password)

                await smtp.send_message(msg)
                await smtp.quit()

                logger.info(
                    "Email sent",
                    extra={
                        "to": msg["To"],
                        "subject": msg["Subject"],
                    },
                )
                return

            except Exception as exc:
                last_error = exc
                if attempt < self._retry.max_retries:
                    delay = self._retry.delay_for_attempt(attempt)
                    logger.warning(
                        "SMTP send failed, retrying",
                        extra={
                            "attempt": attempt + 1,
                            "delay": delay,
                            "error": str(exc),
                        },
                    )
                    await asyncio.sleep(delay)

        raise SMTPError(
            f"Failed to send email after {self._retry.max_retries + 1} attempts: {last_error}"
        )

    def _build_attached_forward(
        self,
        original: EmailMessage,
        subject: str,
        to_address: str,
    ) -> EmailMessage:
        """
        Build a forward message with the original as an attachment.

        Args:
            original: Original message to forward
            subject: Forward subject
            to_address: Recipient
        """
        msg = EmailMessage()
        msg["From"] = self._smtp_user or self._account.username
        msg["To"] = to_address
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=True)

        msg.set_content(f"Forwarded message from {original.get('From', 'unknown')}")

        # Attach original as message/rfc822
        msg.add_attachment(
            original.as_bytes(),
            maintype="message",
            subtype="rfc822",
            filename="forwarded_message.eml",
        )

        return msg

    def _build_inline_forward(
        self,
        original: EmailMessage,
        subject: str,
        to_address: str,
    ) -> EmailMessage:
        """
        Build a forward with original content inline.

        Args:
            original: Original message to forward
            subject: Forward subject
            to_address: Recipient
        """
        msg = EmailMessage()
        msg["From"] = self._smtp_user or self._account.username
        msg["To"] = to_address
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=True)

        # Extract original text body
        body_parts = []
        body_parts.append("---------- Forwarded message ----------")
        body_parts.append(f"From: {original.get('From', '')}")
        body_parts.append(f"Date: {original.get('Date', '')}")
        body_parts.append(f"Subject: {original.get('Subject', '')}")
        body_parts.append(f"To: {original.get('To', '')}")
        body_parts.append("")

        # Get body text
        body = original.get_body(preferencelist=("plain",))
        if body:
            content = body.get_content()
            body_parts.append(content if isinstance(content, str) else str(content))
        else:
            body_parts.append("[No text body available]")

        msg.set_content("\n".join(body_parts))
        return msg


class _SafeFormatDict(dict):
    """Dict subclass that returns the key placeholder for missing keys."""

    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"
