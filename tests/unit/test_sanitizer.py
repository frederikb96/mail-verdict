"""Tests for HTML email sanitizer: tag stripping, image blocking, CID preservation."""

from __future__ import annotations

import pytest

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


class TestCssRemoteContent:
    """A style attribute can fetch a remote resource, and used to.

    Blocking only img src and the background attribute left CSS as an open
    door: background-image, background, list-style-image, border-image,
    content and cursor all take a url(), and every one of them made a request
    when the message was opened. A tracking pixel does not have to be an
    <img>, and a sender who wants to track knows that.
    """

    @pytest.mark.parametrize(
        "css",
        [
            "background-image:url(https://t.test/p.gif)",
            "background:url(https://t.test/p.gif)",
            "list-style-image:url(https://t.test/p.gif)",
            "border-image:url(https://t.test/p.gif)",
            "content:url(https://t.test/p.gif)",
            "cursor:url(https://t.test/p.gif),auto",
        ],
    )
    def test_no_css_property_can_reach_the_network(self, css: str) -> None:
        """Whichever property carries the url(), the fetch is dead."""
        out = sanitize_email_html(f'<div style="{css}">x</div>')
        assert "t.test" not in out.split("data-x-")[0]

    def test_a_quoted_or_single_quoted_url_is_caught_too(self) -> None:
        """Quoting style must not be a way around it."""
        for html in (
            "<div style='background:url(https://t.test/p.gif)'>x</div>",
            '<div style=\'background:url("https://t.test/p.gif")\'>x</div>',
        ):
            assert "t.test" not in sanitize_email_html(html).split("data-x-")[0]

    def test_the_rest_of_the_style_survives(self) -> None:
        """Only the url() is neutralised, so the message still looks right."""
        out = sanitize_email_html(
            '<div style="color:red;background:url(https://t.test/p.gif);margin:4px">x</div>'
        )
        assert "color:red" in out
        assert "margin:4px" in out

    def test_an_inline_attachment_reference_is_left_alone(self) -> None:
        """cid: is the message's own attachment, not a remote fetch."""
        out = sanitize_email_html('<div style="background:url(cid:logo)">x</div>')
        assert "url(cid:logo)" in out
        assert "data-x-style" not in out

    def test_a_style_with_nothing_remote_is_untouched(self) -> None:
        """No url() means nothing to block and nothing to preserve."""
        out = sanitize_email_html('<div style="color:blue">x</div>')
        assert "color:blue" in out
        assert "data-x-style" not in out


class TestAttributeQuotingCannotBypassBlocking:
    """A sender writes the HTML, so they choose the quoting.

    Matching attributes in the raw message means matching every shape a
    sender might produce, and an unquoted attribute slips a pattern written
    for quoted ones -- silently, because the rewrite simply does not fire
    and nothing downstream can tell. Sanitizing first normalises every
    attribute to one form, so there is one shape to match.
    """

    @pytest.mark.parametrize(
        "html",
        [
            '<div style=background-image:url(http://evil.test/t.png)>x</div>',
            '<div style="background-image:url(http://evil.test/t.png)">x</div>',
            "<div style='background:url(http://evil.test/t.png)'>x</div>",
            "<img src=http://evil.test/p.gif>",
            '<img src="http://evil.test/p.gif">',
        ],
    )
    def test_however_it_is_quoted_it_is_blocked(self, html: str) -> None:
        """The remote reference never survives into a live attribute."""
        out = sanitize_email_html(html)
        assert "evil.test" not in out.split("data-x-")[0]


class TestContentCannotEscapeItsBox:
    """A shadow root isolates styles but does not contain layout.

    position:fixed resolves against the viewport, not the element it was
    rendered into -- so a message could cover the entire application. Wrap
    that in a link and the renderer's own click handling turns a click
    anywhere into a navigation the sender chose.
    """

    @pytest.mark.parametrize(
        "declaration",
        [
            "position:fixed", "position:absolute", "position:sticky",
            "z-index:99999", "transform:translate(0,-100px)",
            "top:0", "left:0", "inset:0",
        ],
    )
    def test_escaping_declarations_are_dropped(self, declaration: str) -> None:
        """Each is removed rather than inspected for a safe value."""
        prop = declaration.split(":")[0]
        out = sanitize_email_html(f'<div style="{declaration};color:red">x</div>')
        assert prop not in out
        assert "color:red" in out, "ordinary layout must survive"

    def test_a_full_page_overlay_is_defused(self) -> None:
        """The whole shape, not just one property of it."""
        out = sanitize_email_html(
            '<a href="http://evil.test/"><div style="position:fixed;top:0;left:0;'
            'width:100vw;height:100vh;z-index:9">Click</div></a>'
        )
        assert "position" not in out
        assert "z-index" not in out

    def test_allowing_images_does_not_revive_them(self) -> None:
        """The preserved original is the stripped one, so restoring is safe."""
        from mail_verdict.core.image_sanitizer import restore_remote_images

        out = sanitize_email_html(
            '<div style="position:fixed;background:url(http://evil.test/p.gif)">x</div>'
        )
        assert "position" not in restore_remote_images(out)
