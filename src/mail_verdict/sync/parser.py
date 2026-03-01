"""
Mail parser for extracting structured data from raw email messages.

Uses stdlib email.parser for message structure and parses
Authentication-Results headers for DKIM/SPF/DMARC signals.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AuthResult:
    """Parsed authentication results from Authentication-Results header."""

    dkim_pass: bool | None = None
    spf_pass: bool | None = None
    dmarc_pass: bool | None = None


@dataclass
class AttachmentData:
    """Extracted attachment metadata and content."""

    filename: str | None
    content_type: str | None
    content_id: str | None
    size_bytes: int
    data: bytes


@dataclass
class ParsedMail:
    """Structured email data extracted from raw message."""

    message_id: str | None = None
    subject: str | None = None
    from_addr: str | None = None
    to_addrs: list[str] = field(default_factory=list)
    cc_addrs: list[str] = field(default_factory=list)
    bcc_addrs: list[str] = field(default_factory=list)
    in_reply_to: str | None = None
    date: datetime | None = None
    body_text: str | None = None
    body_html: str | None = None
    raw_headers: dict[str, Any] = field(default_factory=dict)
    auth: AuthResult = field(default_factory=AuthResult)
    attachments: list[AttachmentData] = field(default_factory=list)
    size_bytes: int = 0


def _parse_address_list(header_value: str | None) -> list[str]:
    """
    Parse a comma-separated address header into individual addresses.

    Args:
        header_value: Raw header value (e.g., "Alice <a@x.com>, b@y.com")
    """
    if not header_value:
        return []

    addresses: list[str] = []
    for _name, addr in email.utils.getaddresses([header_value]):
        if addr:
            addresses.append(addr)
    return addresses


def _parse_date(header_value: str | None) -> datetime | None:
    """
    Parse a Date header into a timezone-aware datetime.

    Args:
        header_value: Raw Date header string
    """
    if not header_value:
        return None

    try:
        parsed = email.utils.parsedate_to_datetime(header_value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (ValueError, TypeError):
        logger.debug("Failed to parse date header", extra={"value": header_value})
        return None


def _extract_body(msg: EmailMessage) -> tuple[str | None, str | None]:
    """
    Extract plain text and HTML body from a message.

    Args:
        msg: Parsed EmailMessage

    Returns:
        Tuple of (text_body, html_body)
    """
    text_body: str | None = None
    html_body: str | None = None

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))

            if "attachment" in disposition:
                continue

            if content_type == "text/plain" and text_body is None:
                payload = part.get_content()
                if isinstance(payload, str):
                    text_body = payload
                elif isinstance(payload, bytes):
                    text_body = payload.decode(errors="replace")

            elif content_type == "text/html" and html_body is None:
                payload = part.get_content()
                if isinstance(payload, str):
                    html_body = payload
                elif isinstance(payload, bytes):
                    html_body = payload.decode(errors="replace")
    else:
        content_type = msg.get_content_type()
        payload = msg.get_content()
        content = (
            payload
            if isinstance(payload, str)
            else (payload.decode(errors="replace") if isinstance(payload, bytes) else None)
        )

        if content_type == "text/plain":
            text_body = content
        elif content_type == "text/html":
            html_body = content

    return text_body, html_body


def _extract_attachments(msg: EmailMessage) -> list[AttachmentData]:
    """
    Extract attachments from a message.

    Args:
        msg: Parsed EmailMessage
    """
    attachments: list[AttachmentData] = []

    if not msg.is_multipart():
        return attachments

    for part in msg.walk():
        disposition = str(part.get("Content-Disposition", ""))
        content_type = part.get_content_type()

        is_attachment = "attachment" in disposition or (
            content_type
            not in (
                "text/plain",
                "text/html",
                "multipart/mixed",
                "multipart/alternative",
                "multipart/related",
            )
            and part.get_filename()
        )

        if not is_attachment:
            continue

        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None

        if payload is None:
            continue

        if isinstance(payload, bytes):
            data = payload
        elif isinstance(payload, str):
            data = payload.encode()
        else:
            continue

        attachments.append(
            AttachmentData(
                filename=part.get_filename(),
                content_type=content_type,
                content_id=part.get("Content-ID"),
                size_bytes=len(data),
                data=data,
            )
        )

    return attachments


# Patterns for parsing Authentication-Results header values
_AUTH_METHOD_RE = re.compile(
    r"(dkim|spf|dmarc)\s*=\s*(pass|fail|softfail|neutral|temperror|permerror|none|policy)",
    re.IGNORECASE,
)


def _parse_auth_results(msg: EmailMessage) -> AuthResult:
    """
    Parse Authentication-Results headers for DKIM/SPF/DMARC.

    Handles multiple Authentication-Results headers. For each method,
    'pass' means True, anything else means False, missing means None.

    Args:
        msg: Parsed EmailMessage
    """
    result = AuthResult()

    auth_headers = msg.get_all("Authentication-Results", [])
    if not auth_headers:
        return result

    for header in auth_headers:
        header_str = str(header) if header else ""

        for match in _AUTH_METHOD_RE.finditer(header_str):
            method = match.group(1).lower()
            outcome = match.group(2).lower()
            is_pass = outcome == "pass"

            if method == "dkim" and result.dkim_pass is None:
                result.dkim_pass = is_pass
            elif method == "spf" and result.spf_pass is None:
                result.spf_pass = is_pass
            elif method == "dmarc" and result.dmarc_pass is None:
                result.dmarc_pass = is_pass

    return result


def _extract_raw_headers(msg: EmailMessage) -> dict[str, Any]:
    """
    Extract all headers as a JSON-serializable dict.

    Multiple headers with the same name become a list.

    Args:
        msg: Parsed EmailMessage
    """
    headers: dict[str, Any] = {}

    for key in msg.keys():
        values = msg.get_all(key, [])
        str_values = [str(v) for v in values]

        if len(str_values) == 1:
            headers[key] = str_values[0]
        else:
            headers[key] = str_values

    return headers


def parse_message(raw_bytes: bytes) -> ParsedMail:
    """
    Parse a raw email message into structured data.

    Uses email.policy.default for modern header parsing behavior.

    Args:
        raw_bytes: Raw RFC 2822 message bytes

    Returns:
        Fully parsed mail data
    """
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)

    if not isinstance(msg, EmailMessage):
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)

    text_body, html_body = _extract_body(msg)

    from_header = msg.get("From", "")
    from_addrs = _parse_address_list(from_header)
    from_addr = from_addrs[0] if from_addrs else str(from_header) if from_header else None

    return ParsedMail(
        message_id=msg.get("Message-ID"),
        subject=msg.get("Subject"),
        from_addr=from_addr,
        to_addrs=_parse_address_list(msg.get("To")),
        cc_addrs=_parse_address_list(msg.get("Cc")),
        bcc_addrs=_parse_address_list(msg.get("Bcc")),
        in_reply_to=msg.get("In-Reply-To"),
        date=_parse_date(msg.get("Date")),
        body_text=text_body,
        body_html=html_body,
        raw_headers=_extract_raw_headers(msg),
        auth=_parse_auth_results(msg),
        attachments=_extract_attachments(msg),
        size_bytes=len(raw_bytes),
    )
