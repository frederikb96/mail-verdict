"""Tests for Content-Disposition header encoding of non-Latin-1 filenames."""

from __future__ import annotations

from mail_verdict.core.content_disposition import content_disposition


class TestContentDisposition:
    """Header values must stay Latin-1-encodable however the filename looks."""

    def test_ascii_filename_round_trips(self) -> None:
        header = content_disposition("invoice.pdf")
        assert 'filename="invoice.pdf"' in header
        header.encode("latin-1")  # would raise if not encodable

    def test_non_latin1_filename_does_not_raise(self) -> None:
        """A CJK filename is exactly what breaks a plain filename="..." header."""
        header = content_disposition("请假条.pdf")
        header.encode("latin-1")  # the bug: this used to raise UnicodeEncodeError

    def test_non_latin1_filename_carries_rfc5987_form(self) -> None:
        header = content_disposition("请假条.pdf")
        assert "filename*=UTF-8''" in header
        assert "%" in header.split("filename*=UTF-8''")[1]

    def test_non_latin1_filename_has_ascii_fallback(self) -> None:
        header = content_disposition("Rëçü_😀.pdf")
        fallback = header.split('filename="')[1].split('"')[0]
        fallback.encode("ascii")  # would raise if the fallback still isn't ASCII

    def test_control_characters_stripped(self) -> None:
        header = content_disposition("evil\r\nX-Injected: true.txt")
        assert "\r" not in header
        assert "\n" not in header

    def test_inline_disposition(self) -> None:
        header = content_disposition("preview.png", disposition="inline")
        assert header.startswith("inline;")
