"""Tests for HTML email sanitizer: tag stripping, image blocking, CID preservation."""

from __future__ import annotations

import pytest

from mail_verdict.core.sanitizer import declares_dark_mode_support, sanitize_email_html


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


class TestCssParsingCannotBeSyntaxedAround:
    """Matching a property name with `declaration.split(":", 1)[0]` is not
    how CSS is parsed, so it is not how CSS has to be written either.

    A comment between the name and the colon, or a hex escape inside the
    name, both parse as an ordinary declaration in every browser and both
    slipped past a string-split check untouched -- proven against a real
    shadow root in Chrome: the message covered the full viewport at
    z-index 2147483647 and took every click in the application.
    """

    def test_a_css_comment_cannot_hide_the_property_name(self) -> None:
        """top/**/:0 is `top:0` to a browser, and must be to the sanitizer too."""
        out = sanitize_email_html('<div style="top/**/:0;color:red">x</div>')
        assert "top" not in out
        assert "color:red" in out

    def test_a_hex_escape_cannot_hide_the_property_name(self) -> None:
        r"""p\6fsition is `position` to a browser -- \6f is the escape for 'o'."""
        out = sanitize_email_html('<div style="p\\6fsition:fixed;color:red">x</div>')
        assert "fixed" not in out
        assert "color:red" in out

    def test_the_full_proven_overlay_payload_is_defused(self) -> None:
        """The exact combination that took every click over a real shadow root."""
        out = sanitize_email_html(
            '<a href="https://attacker.example/phish" rel="noopener noreferrer">'
            '<div style="p\\6fsition:fixed; top/**/:0; left/**/:0; width:100vw; '
            'height:100vh; z-index/**/:2147483647; background:#ffffff">'
            "<h1>Click</h1></div></a>"
        )
        assert "fixed" not in out
        assert "z-index" not in out
        assert "2147483647" not in out

    def test_a_vendor_prefixed_transform_is_caught_under_its_bare_name(self) -> None:
        """A browser honours -webkit-transform exactly like transform."""
        out = sanitize_email_html(
            '<div style="-webkit-transform:translate(0,-9999px);color:red">x</div>'
        )
        assert "transform" not in out
        assert "color:red" in out

    def test_an_escaped_url_function_name_cannot_hide_a_tracking_pixel(self) -> None:
        r"""ur\6c( is `url(` to a browser -- the same escape, not the same target."""
        out = sanitize_email_html('<div style="background:ur\\6c(https://evil.test/p.gif)">x</div>')
        assert "evil.test" not in out.split("data-x-")[0]


class TestMalformedCssCannotBreakOutOfTheAttribute:
    """A style value is spliced back into markup, so whatever the tokenizer
    hands back has to be escaped there.

    A CSS tokenizer recovering from malformed input echoes the malformed
    text back rather than discarding it, and an unterminated string comes
    back carrying the quote that opened it and nothing that closes it.
    Spliced unescaped, that quote ends the attribute early and every
    character after it becomes markup -- on an element that has already
    passed the tag and attribute allowlist, so an event handler arrives on
    a tag nothing approved it for. The backend's contract is that its
    output is safe to embed anywhere, and `GET /api/mails/{id}` hands
    `body_html` to any API consumer, so this cannot rest on the browser
    client happening to sanitize a second time.

    Only the single-quoted CSS string reaches this: nh3 escapes a double
    quote to an entity before the rewrite sees it, so the other shapes here
    cover the neighbourhood rather than reproduce the defect.
    """

    @staticmethod
    def _attributes(html: str) -> dict[str, str | None]:
        """Every attribute a real HTML parser sees on the first tag."""
        from html.parser import HTMLParser

        found: dict[str, str | None] = {}

        class Collector(HTMLParser):
            def handle_starttag(
                self, tag: str, attrs: list[tuple[str, str | None]],
            ) -> None:
                found.update(dict(attrs))

        Collector().feed(html)
        return found

    @pytest.mark.parametrize(
        "html",
        [
            pytest.param(
                '<img src="cid:x" style="background:url(\'cid: '
                'onerror=alert(1) foo=bar" alt="x">',
                id="unterminated-single-quote",
            ),
            pytest.param(
                '<img src="cid:x" style=\'background:url("cid: '
                "onerror=alert(1) foo=bar' alt=\"x\">",
                id="unterminated-double-quote",
            ),
            pytest.param(
                '<div style="background:url(\'http://evil.test/t.gif '
                'onerror=alert(1) x=y">hi</div>',
                id="unterminated-on-the-remote-url-branch",
            ),
        ],
    )
    def test_an_unterminated_string_cannot_inject_an_attribute(self, html: str) -> None:
        attributes = self._attributes(sanitize_email_html(html))
        injected = [name for name in attributes if name.startswith("on")]
        assert not injected, f"event handler injected: {injected} in {attributes}"

    def test_a_legitimate_remote_url_still_round_trips(self) -> None:
        """The escaping must not cost the image-consent layer its original."""
        cleaned = sanitize_email_html(
            '<div style="color:red;background:url(&quot;http://evil.test/p.gif&quot;)">'
            "ok</div>"
        )
        attributes = self._attributes(cleaned)
        assert "data-x-style" in attributes
        assert "about:blank" in (attributes.get("style") or "")


class TestQuotedCssStringsSurviveSanitization:
    """nh3 runs first and entity-encodes a `"` inside an attribute value as
    &quot; -- captured verbatim, that text still carries the `;` from
    inside the quote, so a quoted font stack tokenises into garbage and
    parse errors unless it is unescaped before tinycss2 ever parses it.
    Quoted font-family is the single most common style pattern in real
    newsletters, so this is not a corner case.
    """

    def test_a_quoted_font_family_round_trips(self) -> None:
        out = sanitize_email_html(
            '<div style=\'font-family:"Open Sans", Arial; color:red\'>x</div>'
        )
        attributes = TestMalformedCssCannotBreakOutOfTheAttribute._attributes(out)
        style = attributes.get("style") or ""
        assert "Open Sans" in style
        assert "color:red" in style
        # Never double-escaped -- exactly one round of entity-encoding.
        assert "&amp;quot;" not in out

    def test_a_quoted_content_value_round_trips(self) -> None:
        out = sanitize_email_html('<div style=\'content:"→"\'>x</div>')
        attributes = TestMalformedCssCannotBreakOutOfTheAttribute._attributes(out)
        assert "→" in (attributes.get("style") or "")

    def test_a_quoted_remote_url_inside_a_double_quoted_attribute_is_still_blocked(
        self,
    ) -> None:
        """The unescape must not reopen the remote-url hole the escaping
        exists to close -- only the round-trip of harmless text changes."""
        out = sanitize_email_html(
            '<div style="background:url(&quot;https://evil.test/p.gif&quot;)">x</div>'
        )
        assert "evil.test" not in out.split("data-x-")[0]


class TestUrlStringFunctionsCannotEvadeDetection:
    """A remote reference does not have to be a url() token -- src() (a
    proposed general url() alternative) and image() (CSS Images level 4)
    both take a plain string argument, so a check keyed on url tokens
    alone misses them."""

    @pytest.mark.parametrize("fn", ["src", "image"])
    def test_a_remote_reference_via_the_function_is_neutralised(self, fn: str) -> None:
        out = sanitize_email_html(
            f'<div style="background:{fn}(&quot;https://evil.test/p.gif&quot;)">x</div>'
        )
        assert "evil.test" not in out.split("data-x-")[0]

    @pytest.mark.parametrize("fn", ["src", "image"])
    def test_a_local_reference_via_the_function_is_left_alone(self, fn: str) -> None:
        out = sanitize_email_html(f'<div style="background:{fn}(&quot;cid:logo&quot;)">x</div>')
        assert "cid:logo" in out
        assert "data-x-style" not in out


class TestStructuralTagsDoNotLeakTheirTextAsCopy:
    """A tag outside the allowlist has its tag stripped, but by default
    only script and style also lose their text -- every other disallowed
    tag is unwrapped, hoisting its text into the surrounding body. An
    ESP-generated message routinely carries a `<head>` full of exactly
    this kind of markup ahead of the real content, so the leaked text
    lands at the very top of what renders."""

    def test_a_title_does_not_become_visible_copy(self) -> None:
        html = "<head><title>Weekly newsletter</title></head><body><p>Hello</p></body>"
        out = sanitize_email_html(html)
        assert "Weekly newsletter" not in out
        assert "<p>Hello</p>" in out

    def test_a_head_full_of_metadata_does_not_become_visible_copy(self) -> None:
        html = (
            "<html><head>"
            '<meta name="viewport" content="width=device-width">'
            "<title>Newsletter</title>"
            "<style>body{margin:0}</style>"
            "</head><body><p>Real content</p></body></html>"
        )
        out = sanitize_email_html(html)
        assert "Newsletter" not in out
        assert "margin" not in out
        assert "<p>Real content</p>" in out

    def test_an_outlook_xml_block_does_not_leak(self) -> None:
        html = "<xml><o:p>Office namespace junk</o:p></xml><p>after</p>"
        out = sanitize_email_html(html)
        assert "Office namespace junk" not in out
        assert "<p>after</p>" in out

    def test_a_style_hidden_behind_the_legacy_comment_trick_still_does_not_leak(
        self,
    ) -> None:
        """<style><!-- ... --></style> is how old newsletters hide CSS from
        browsers that do not understand <style> -- the CSS must not leak
        either as markup or, once unwrapped, as visible text."""
        html = "<style><!--\nbody { color: red; }\n--></style><p>ok</p>"
        out = sanitize_email_html(html)
        assert "color" not in out
        assert "<p>ok</p>" in out

    def test_an_ordinary_unknown_tag_still_keeps_its_text(self) -> None:
        """The fix must not become a general content-eater: a stray or
        custom inline tag with no security meaning still shows its text,
        exactly as before."""
        html = "<blink>Flashy but harmless</blink>"
        out = sanitize_email_html(html)
        assert "Flashy but harmless" in out

    def test_iframe_fallback_text_does_not_leak_either(self) -> None:
        html = '<iframe src="https://tracker.example/x">no iframe support</iframe><p>ok</p>'
        out = sanitize_email_html(html)
        assert "no iframe support" not in out
        assert "tracker.example" not in out
        assert "<p>ok</p>" in out


class TestDarkModeDeclaration:
    """A message can say it renders correctly on a dark canvas, through any
    of the three shapes mail actually uses. The signal only exists in the
    head/meta/style markup this module otherwise discards, so it has to be
    read from the raw HTML before sanitizing."""

    def test_the_css_color_scheme_meta_tag_is_recognised(self) -> None:
        html = '<meta name="color-scheme" content="light dark"><p>x</p>'
        assert declares_dark_mode_support(html) is True

    def test_apples_supported_color_schemes_meta_tag_is_recognised(self) -> None:
        html = '<meta name="supported-color-schemes" content="light dark"><p>x</p>'
        assert declares_dark_mode_support(html) is True

    def test_the_color_scheme_css_property_is_recognised(self) -> None:
        html = "<style>:root{color-scheme: light dark;}</style><p>x</p>"
        assert declares_dark_mode_support(html) is True

    def test_a_prefers_color_scheme_media_query_is_recognised(self) -> None:
        html = (
            "<style>@media (prefers-color-scheme: dark) "
            "{ body { background:#000 } }</style><p>x</p>"
        )
        assert declares_dark_mode_support(html) is True

    def test_ordinary_mail_with_no_signal_is_not_mistaken_for_dark_aware(self) -> None:
        html = "<head><title>Newsletter</title></head><body><p>Hello</p></body>"
        assert declares_dark_mode_support(html) is False

    def test_a_light_only_declaration_is_not_treated_as_dark_support(self) -> None:
        html = '<meta name="color-scheme" content="light only"><p>x</p>'
        assert declares_dark_mode_support(html) is False


class TestDataImagesRenderAsDocumented:
    """A data: image is embedded, not a network fetch -- _LOCAL_URL_PREFIXES
    already says so, but nh3's url_schemes dropped the src attribute
    outright before that preservation logic ever got to see it, so the
    documented "preserved" behaviour never actually rendered anything."""

    def test_a_data_uri_image_survives_untouched(self) -> None:
        out = sanitize_email_html('<img src="data:image/png;base64,AAAA" alt="x">')
        assert 'src="data:image/png;base64,AAAA"' in out
        assert "data-x-src" not in out

    def test_a_data_uri_background_survives_untouched(self) -> None:
        out = sanitize_email_html(
            '<table><tr><td background="data:image/png;base64,AAAA">x</td></tr></table>'
        )
        assert 'background="data:image/png;base64,AAAA"' in out
        assert "data-x-bg" not in out

    def test_a_remote_image_is_still_blocked(self) -> None:
        """Allowing the data: scheme through must not reopen the remote
        case the rewrite exists for."""
        out = sanitize_email_html('<img src="https://tracker.example.com/pixel.gif">')
        assert "data-x-src=" in out
        assert ' src="https' not in out
