"""Tests for HTML email sanitizer: tag stripping, image blocking, CID preservation."""

from __future__ import annotations

from mail_verdict.core.sanitizer import sanitize_email_html


class TestRemoteImageBlocking:
    """Tests for remote image URL rewriting."""

    def test_blocks_http_image(self) -> None:
        """HTTP image src is rewritten to data-x-src."""
        html = '<img src="http://tracker.example.com/pixel.gif">'
        result = sanitize_email_html(html)
        assert ' src="http' not in result
        assert "data-x-src=" in result
        assert "tracker.example.com" in result

    def test_blocks_https_image(self) -> None:
        """HTTPS image src is rewritten to data-x-src."""
        html = '<img src="https://cdn.example.com/logo.png" alt="logo">'
        result = sanitize_email_html(html)
        assert "data-x-src=" in result
        assert 'alt="logo"' in result

    def test_preserves_cid_image(self) -> None:
        """CID references are NOT rewritten (inline MIME images are safe)."""
        html = '<img src="cid:image001@example.com">'
        result = sanitize_email_html(html)
        assert "cid:image001@example.com" in result

    def test_blocks_background_attribute(self) -> None:
        """Background attribute URLs are rewritten to data-x-bg."""
        html = '<table><tr><td background="http://example.com/bg.jpg">cell</td></tr></table>'
        result = sanitize_email_html(html)
        assert "data-x-bg=" in result
        assert 'background=' not in result

    def test_single_quoted_src(self) -> None:
        """Single-quoted src is also rewritten."""
        html = "<img src='http://tracker.example.com/pixel.gif'>"
        result = sanitize_email_html(html)
        assert "data-x-src=" in result


class TestDangerousTagRemoval:
    """Tests for stripping dangerous HTML elements."""

    def test_strips_script_tags(self) -> None:
        """Script tags are removed."""
        html = '<p>Hello</p><script>alert("xss")</script>'
        result = sanitize_email_html(html)
        assert "<script" not in result
        assert "alert" not in result

    def test_strips_iframe(self) -> None:
        """Iframe tags are removed."""
        html = '<iframe src="http://evil.com"></iframe><p>Safe</p>'
        result = sanitize_email_html(html)
        assert "<iframe" not in result
        assert "Safe" in result

    def test_strips_embed(self) -> None:
        """Embed tags are removed."""
        html = '<embed src="flash.swf"><p>text</p>'
        result = sanitize_email_html(html)
        assert "<embed" not in result

    def test_strips_object(self) -> None:
        """Object tags are removed."""
        html = '<object data="malware.exe"></object><p>ok</p>'
        result = sanitize_email_html(html)
        assert "<object" not in result

    def test_strips_form(self) -> None:
        """Form tags are removed (phishing prevention)."""
        html = '<form action="http://evil.com"><input type="text"></form>'
        result = sanitize_email_html(html)
        assert "<form" not in result


class TestSafeTagPreservation:
    """Tests for preserving safe formatting tags."""

    def test_preserves_basic_formatting(self) -> None:
        """Basic formatting tags pass through."""
        html = "<p><strong>Bold</strong> and <em>italic</em></p>"
        result = sanitize_email_html(html)
        assert "<strong>" in result
        assert "<em>" in result
        assert "<p>" in result

    def test_preserves_links(self) -> None:
        """Anchor tags are preserved with safe attributes."""
        html = '<a href="https://example.com" title="link">Click</a>'
        result = sanitize_email_html(html)
        assert "<a " in result
        assert 'href="https://example.com"' in result

    def test_preserves_tables(self) -> None:
        """Table tags pass through."""
        html = "<table><tr><td>Cell</td></tr></table>"
        result = sanitize_email_html(html)
        assert "<table>" in result
        assert "<td>" in result

    def test_preserves_lists(self) -> None:
        """List tags pass through."""
        html = "<ul><li>Item 1</li><li>Item 2</li></ul>"
        result = sanitize_email_html(html)
        assert "<ul>" in result
        assert "<li>" in result


class TestEdgeCases:
    """Tests for edge cases."""

    def test_empty_string(self) -> None:
        """Empty string returns empty."""
        assert sanitize_email_html("") == ""

    def test_plain_text(self) -> None:
        """Plain text passes through."""
        result = sanitize_email_html("Hello, world!")
        assert "Hello, world!" in result

    def test_mixed_safe_and_dangerous(self) -> None:
        """Mixed content preserves safe, strips dangerous."""
        html = (
            '<p>Hello</p>'
            '<script>evil()</script>'
            '<img src="http://tracker.com/px.gif">'
            '<a href="https://safe.com">link</a>'
        )
        result = sanitize_email_html(html)
        assert "<p>" in result
        assert "<script" not in result
        assert "data-x-src=" in result
        assert "<a " in result
