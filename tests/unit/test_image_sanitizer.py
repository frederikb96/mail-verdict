"""Tests for read-time image sanitizer: stripping, restoring, email/domain extraction."""

from __future__ import annotations

from mail_verdict.core.image_sanitizer import (
    extract_sender_domain,
    extract_sender_email,
    restore_remote_images,
    strip_remote_images,
)


class TestStripRemoteImages:
    """Tests for stripping remote images from HTML."""

    def test_strips_http_img(self) -> None:
        """HTTP img tags are removed."""
        html = '<p>Hello</p><img src="http://tracker.com/pixel.gif"><p>end</p>'
        result, has_remote = strip_remote_images(html)
        assert "<img" not in result
        assert has_remote is True
        assert "Hello" in result

    def test_strips_https_img(self) -> None:
        """HTTPS img tags are removed."""
        html = '<img src="https://cdn.example.com/logo.png" alt="logo">'
        result, has_remote = strip_remote_images(html)
        assert "<img" not in result
        assert has_remote is True

    def test_preserves_data_uri(self) -> None:
        """Data URI images are preserved."""
        html = '<img src="data:image/png;base64,iVBOR...">'
        result, has_remote = strip_remote_images(html)
        assert "data:image/png" in result
        assert has_remote is False

    def test_preserves_cid_image(self) -> None:
        """CID references are preserved."""
        html = '<img src="cid:image001@example.com">'
        result, has_remote = strip_remote_images(html)
        assert "cid:image001@example.com" in result
        assert has_remote is False

    def test_strips_data_x_src(self) -> None:
        """Store-time sanitized data-x-src images are detected and stripped."""
        html = '<img data-x-src="https://tracker.com/pixel.gif">'
        result, has_remote = strip_remote_images(html)
        assert "<img" not in result
        assert has_remote is True

    def test_no_images_returns_false(self) -> None:
        """No images means has_remote is False."""
        html = "<p>Plain text email</p>"
        result, has_remote = strip_remote_images(html)
        assert result == html
        assert has_remote is False


class TestRestoreRemoteImages:
    """Tests for restoring data-x-src back to src."""

    def test_restores_data_x_src(self) -> None:
        """data-x-src is converted back to src."""
        html = '<img data-x-src="https://cdn.example.com/logo.png" alt="logo">'
        result = restore_remote_images(html)
        assert 'src="https://cdn.example.com/logo.png"' in result
        assert "data-x-src" not in result

    def test_blocks_javascript_scheme(self) -> None:
        """javascript: URIs in data-x-src are dropped (XSS prevention)."""
        html = '<img data-x-src="javascript:alert(1)" alt="xss">'
        result = restore_remote_images(html)
        assert "javascript:" not in result
        assert "src=" not in result

    def test_blocks_vbscript_scheme(self) -> None:
        """vbscript: URIs in data-x-src are dropped."""
        html = '<img data-x-src="vbscript:MsgBox" alt="xss">'
        result = restore_remote_images(html)
        assert "vbscript:" not in result
        assert "src=" not in result

    def test_allows_http_scheme(self) -> None:
        """http:// URIs are restored normally."""
        html = '<img data-x-src="http://example.com/img.png">'
        result = restore_remote_images(html)
        assert 'src="http://example.com/img.png"' in result

    def test_blocks_data_uri_scheme(self) -> None:
        """data: URIs in data-x-src are dropped (not expected here)."""
        html = '<img data-x-src="data:text/html,<script>alert(1)</script>">'
        result = restore_remote_images(html)
        assert "data:text/html" not in result
        assert "src=" not in result


class TestExtractSenderEmail:
    """Tests for extracting bare email from from_addr."""

    def test_plain_email(self) -> None:
        """Plain email address."""
        assert extract_sender_email("user@example.com") == "user@example.com"

    def test_display_name_format(self) -> None:
        """Name <email> format."""
        assert extract_sender_email("John Doe <john@example.com>") == "john@example.com"

    def test_none_input(self) -> None:
        """None returns None."""
        assert extract_sender_email(None) is None

    def test_empty_string(self) -> None:
        """Empty string returns None."""
        assert extract_sender_email("") is None

    def test_uppercase_normalized(self) -> None:
        """Email is lowercased."""
        assert extract_sender_email("User@Example.COM") == "user@example.com"


class TestExtractSenderDomain:
    """Tests for extracting domain from email address."""

    def test_plain_email(self) -> None:
        """Domain from plain email."""
        assert extract_sender_domain("user@example.com") == "example.com"

    def test_display_name_format(self) -> None:
        """Domain from Name <email> format."""
        assert extract_sender_domain("John <john@github.com>") == "github.com"

    def test_none_input(self) -> None:
        """None returns None."""
        assert extract_sender_domain(None) is None

    def test_subdomain(self) -> None:
        """Subdomain is preserved."""
        assert extract_sender_domain("no-reply@mail.example.com") == "mail.example.com"
