"""Tests for mail parser: header extraction, body parsing, auth results, attachments."""

from __future__ import annotations

from pathlib import Path

from mail_verdict.sync.parser import (
    ParsedMail,
    _parse_address_list,
    _parse_date,
    parse_message,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "emails"


class TestParseAddressList:
    """Tests for _parse_address_list."""

    def test_single_address(self) -> None:
        """Parses a single bare address."""
        assert _parse_address_list("bob@example.com") == ["bob@example.com"]

    def test_display_name_format(self) -> None:
        """Parses 'Name <addr>' format."""
        assert _parse_address_list("Alice <alice@example.com>") == ["alice@example.com"]

    def test_multiple_addresses(self) -> None:
        """Parses comma-separated addresses."""
        result = _parse_address_list("a@x.com, b@y.com")
        assert result == ["a@x.com", "b@y.com"]

    def test_none_input(self) -> None:
        """None returns empty list."""
        assert _parse_address_list(None) == []

    def test_empty_string(self) -> None:
        """Empty string returns empty list."""
        assert _parse_address_list("") == []


class TestParseDate:
    """Tests for _parse_date."""

    def test_valid_rfc2822_date(self) -> None:
        """Parses a standard RFC 2822 date."""
        dt = _parse_date("Mon, 15 Jan 2024 10:00:00 +0000")
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15

    def test_none_input(self) -> None:
        """None returns None."""
        assert _parse_date(None) is None

    def test_invalid_date(self) -> None:
        """Garbage string returns None."""
        assert _parse_date("not a date") is None

    def test_naive_date_gets_utc(self) -> None:
        """Date without timezone gets UTC assigned."""
        dt = _parse_date("Mon, 15 Jan 2024 10:00:00")
        assert dt is not None
        assert dt.tzinfo is not None


class TestParseMessage:
    """Tests for full message parsing."""

    def test_simple_ham(self, sample_email_bytes: bytes) -> None:
        """Parses a simple text/plain email."""
        parsed = parse_message(sample_email_bytes)
        assert isinstance(parsed, ParsedMail)
        assert parsed.subject == "Meeting tomorrow at 3pm"
        assert parsed.from_addr == "alice@example.com"
        assert "bob@example.com" in parsed.to_addrs
        assert parsed.message_id == "<ham-simple-001@example.com>"
        assert parsed.body_text is not None
        assert "Conference Room B" in parsed.body_text

    def test_spam_email(self, sample_spam_bytes: bytes) -> None:
        """Parses a spam email with auth failures."""
        parsed = parse_message(sample_spam_bytes)
        assert "V1agra" in (parsed.subject or "")
        assert parsed.from_addr == "deals@ch3ap-m3ds.xyz"
        assert parsed.auth.dkim_pass is False
        assert parsed.auth.spf_pass is False
        assert parsed.auth.dmarc_pass is False

    def test_auth_results_pass(self, sample_email_bytes: bytes) -> None:
        """All auth methods pass for ham email."""
        parsed = parse_message(sample_email_bytes)
        assert parsed.auth.dkim_pass is True
        assert parsed.auth.spf_pass is True
        assert parsed.auth.dmarc_pass is True

    def test_empty_body(self) -> None:
        """Handles email with no body."""
        raw = (FIXTURES_DIR / "empty_body.eml").read_bytes()
        parsed = parse_message(raw)
        assert parsed.subject is not None
        assert parsed.body_text is None or parsed.body_text.strip() == ""

    def test_unicode_subject(self) -> None:
        """Handles unicode in subject line."""
        raw = (FIXTURES_DIR / "unicode_subject.eml").read_bytes()
        parsed = parse_message(raw)
        assert parsed.subject is not None

    def test_multipart_html(self) -> None:
        """Extracts both text and HTML from multipart message."""
        raw = (FIXTURES_DIR / "multipart_html.eml").read_bytes()
        parsed = parse_message(raw)
        assert parsed.body_text is not None
        assert parsed.body_html is not None

    def test_attachment_detection(self) -> None:
        """Detects attachments in multipart message."""
        raw = (FIXTURES_DIR / "multipart_attachment.eml").read_bytes()
        parsed = parse_message(raw)
        assert len(parsed.attachments) > 0
        att = parsed.attachments[0]
        assert att.filename is not None
        assert att.size_bytes > 0

    def test_no_auth_headers(self) -> None:
        """Missing Authentication-Results leaves auth fields as None."""
        raw = (FIXTURES_DIR / "no_auth.eml").read_bytes()
        parsed = parse_message(raw)
        assert parsed.auth.dkim_pass is None
        assert parsed.auth.spf_pass is None
        assert parsed.auth.dmarc_pass is None

    def test_size_bytes_set(self, sample_email_bytes: bytes) -> None:
        """size_bytes matches raw input length."""
        parsed = parse_message(sample_email_bytes)
        assert parsed.size_bytes == len(sample_email_bytes)

    def test_raw_headers_extracted(self, sample_email_bytes: bytes) -> None:
        """raw_headers dict is populated."""
        parsed = parse_message(sample_email_bytes)
        assert "From" in parsed.raw_headers
        assert "Subject" in parsed.raw_headers
