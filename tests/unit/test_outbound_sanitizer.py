"""Outbound HTML sanitisation -- the boundary that makes composed, pasted
and quoted content safe to send as mail."""

from __future__ import annotations

from mail_verdict.core.outbound_sanitizer import sanitize_outbound_html


class TestAllowlist:
    def test_script_and_event_handlers_are_stripped(self) -> None:
        html = '<p onclick="alert(1)">hi</p><script>alert(2)</script>'
        out = sanitize_outbound_html(html)
        assert "onclick" not in out
        assert "<script" not in out
        assert "hi" in out

    def test_class_and_style_do_not_survive_from_input(self) -> None:
        html = '<p class="fancy" style="color:red">styled</p>'
        out = sanitize_outbound_html(html)
        assert "class=" not in out
        assert "color:red" not in out

    def test_positioning_declarations_are_dropped(self) -> None:
        html = '<p style="position:fixed;top:0">escaping</p>'
        out = sanitize_outbound_html(html)
        assert "position" not in out
        assert "top" not in out

    def test_a_non_mail_url_scheme_is_stripped(self) -> None:
        html = '<a href="javascript:alert(1)">bad</a>'
        out = sanitize_outbound_html(html)
        assert "javascript:" not in out

    def test_an_http_link_survives(self) -> None:
        html = '<a href="https://example.com">link</a>'
        out = sanitize_outbound_html(html)
        assert 'href="https://example.com"' in out

    def test_rel_and_target_are_not_injected(self) -> None:
        html = '<a href="https://example.com">link</a>'
        out = sanitize_outbound_html(html)
        assert "rel=" not in out
        assert "target=" not in out

    def test_tables_survive_for_quoted_mail(self) -> None:
        html = "<table><tr><td>cell</td></tr></table>"
        out = sanitize_outbound_html(html)
        assert "<table>" in out
        assert "<td>cell</td>" in out


class TestImages:
    def test_a_remote_image_survives(self) -> None:
        html = '<img src="https://example.com/a.png" alt="a">'
        out = sanitize_outbound_html(html)
        assert 'src="https://example.com/a.png"' in out

    def test_a_cid_image_is_dropped_entirely_by_default(self) -> None:
        """Nothing is attached to the outgoing message for a cid: reference
        to resolve against, so the tag disappears rather than being left
        as a broken image -- the shape a quoted original's own inline
        image is in, which api/mails.py's quote endpoint never re-attaches."""
        html = '<p>before</p><img src="cid:abc123"><p>after</p>'
        out = sanitize_outbound_html(html)
        assert "<img" not in out
        assert "before" in out
        assert "after" in out

    def test_a_cid_image_survives_with_allow_cid(self) -> None:
        """The compose API's own call site opts in for exactly the case a
        cid: reference is real: the compose editor's own inline image,
        with the matching outbox_attachments row inserted alongside it in
        the same request."""
        html = '<p>before</p><img src="cid:abc123" alt="a"><p>after</p>'
        out = sanitize_outbound_html(html, allow_cid=True)
        assert 'src="cid:abc123"' in out
        assert "before" in out
        assert "after" in out

    def test_an_image_keeps_its_resized_dimensions(self) -> None:
        html = '<img src="cid:abc123" width="200" height="100">'
        out = sanitize_outbound_html(html, allow_cid=True)
        assert 'width="200"' in out
        assert 'height="100"' in out

    def test_a_local_attachment_url_is_dropped_entirely(self) -> None:
        """The display-only /api/messages/... URL a message's own detail
        view carries means nothing to a message actually being sent."""
        html = '<img src="/api/messages/123/attachments/456">'
        out = sanitize_outbound_html(html)
        assert "<img" not in out


class TestQuoteStyling:
    def test_blockquote_gets_the_quote_bar_whatever_style_it_carried(self) -> None:
        html = '<blockquote type="cite" style="border:none">quoted</blockquote>'
        out = sanitize_outbound_html(html)
        assert 'type="cite"' in out
        assert "border-left:1px #ccc solid" in out

    def test_a_bare_blockquote_also_gets_the_bar(self) -> None:
        html = "<blockquote>quoted</blockquote>"
        out = sanitize_outbound_html(html)
        assert "border-left:1px #ccc solid" in out

    def test_pre_gets_a_monospace_background(self) -> None:
        html = "<pre><code>x = 1</code></pre>"
        out = sanitize_outbound_html(html)
        assert "font-family:monospace" in out


class TestQuoteMarkerClasses:
    def test_the_gmail_quote_and_attr_classes_survive_by_exact_value(self) -> None:
        html = '<div class="gmail_quote"><div class="gmail_attr">On x wrote:</div>' \
            '<blockquote type="cite" class="gmail_quote">quoted</blockquote></div>'
        out = sanitize_outbound_html(html)
        assert 'class="gmail_quote"' in out
        assert 'class="gmail_attr"' in out

    def test_any_other_class_value_is_dropped_even_when_it_contains_the_marker(self) -> None:
        """nh3 matches by whole value, not per token -- a quoted message
        combining its own class with the marker must not slip through."""
        html = '<div class="gmail_quote evil-tracker">x</div>'
        out = sanitize_outbound_html(html)
        assert "class=" not in out

    def test_an_unrelated_class_is_dropped(self) -> None:
        html = '<div class="newsletter-header">x</div>'
        out = sanitize_outbound_html(html)
        assert "class=" not in out


class TestQuotedMessageMarker:
    def test_the_data_quoted_message_marker_survives(self) -> None:
        """Without this, a draft carrying a quote loses the attribute the
        compose editor's quotedMessage node parses back on reopening --
        the quote would still render, but never re-collapse into the node."""
        html = '<div data-quoted-message="true" class="gmail_quote">x</div>'
        out = sanitize_outbound_html(html)
        assert 'data-quoted-message="true"' in out

    def test_any_other_data_quoted_message_value_is_dropped(self) -> None:
        html = '<div data-quoted-message="false">x</div>'
        out = sanitize_outbound_html(html)
        assert "data-quoted-message" not in out


class TestListItemUnwrap:
    def test_a_paragraph_wrapped_list_item_is_unwrapped(self) -> None:
        """Outlook and Gmail both apply their own per-<p> margin, which
        without this renders as a blank line between every bullet."""
        html = "<ul><li><p>item</p></li></ul>"
        out = sanitize_outbound_html(html)
        assert out == "<ul><li>item</li></ul>"

    def test_a_nested_list_survives_the_unwrap(self) -> None:
        html = "<ul><li><p>outer</p><ul><li><p>inner</p></li></ul></li></ul>"
        out = sanitize_outbound_html(html)
        assert "<li>outer<ul><li>inner</li></ul></li>" in out
