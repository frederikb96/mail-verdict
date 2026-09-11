"""The mail list row's snippet -- boilerplate lines filtered out before
truncation, so a reply's own quoted header or a newsletter's tracking link
never crowds out the one line a reader would actually want to see."""

from __future__ import annotations

from mail_verdict.core.snippet import build_snippet


class TestSeparatorLines:
    def test_a_run_of_underscores_is_dropped(self) -> None:
        body = "________________________________\nVon: someone@example.com"
        # Both the separator and the forwarded header below it are noise --
        # nothing left, so the snippet is empty.
        assert build_snippet(body) is None

    def test_dashes_and_equals_are_dropped_too(self) -> None:
        assert build_snippet("---\nHello there\n===") == "Hello there"


class TestForwardedHeaderBlocks:
    def test_german_forward_header_is_dropped(self) -> None:
        body = "Von: Anna\nGesendet: Montag\nAn: Bob\nBetreff: Hallo\nDer eigentliche Text"
        assert build_snippet(body) == "Der eigentliche Text"

    def test_english_forward_header_is_dropped(self) -> None:
        body = "From: Anna\nSent: Monday\nTo: Bob\nSubject: Hi\nThe actual message"
        assert build_snippet(body) == "The actual message"


class TestViewInBrowser:
    def test_view_this_email_in_your_browser(self) -> None:
        body = (
            "View this email in your browser (https://us8.campaign-archive.com/xyz)\n"
            "Real content here"
        )
        assert build_snippet(body) == "Real content here"

    def test_view_this_post_on_the_web(self) -> None:
        body = "View this post on the web at https://example.com/post\nThe newsletter body"
        assert build_snippet(body) == "The newsletter body"

    def test_german_im_browser_lesen(self) -> None:
        body = "Im Browser lesen\nDer Newsletter-Inhalt"
        assert build_snippet(body) == "Der Newsletter-Inhalt"


class TestBareUrls:
    def test_a_line_that_is_only_a_url_is_dropped(self) -> None:
        body = "https://example.com/tracking/xyz\nThe real preview text"
        assert build_snippet(body) == "The real preview text"

    def test_a_parenthesised_bare_url_line_is_dropped(self) -> None:
        body = "(https://example.com/track)\nSome content"
        assert build_snippet(body) == "Some content"

    def test_a_url_inline_with_other_text_is_kept(self) -> None:
        # Only a line that is *nothing but* a URL is noise -- one mentioned
        # inline as part of a real sentence is still real content.
        body = "See https://example.com for details"
        assert build_snippet(body) == "See https://example.com for details"


class TestOrdinaryText:
    def test_a_plain_body_is_unaffected(self) -> None:
        assert build_snippet("Hi, just checking in about tomorrow.") == (
            "Hi, just checking in about tomorrow."
        )

    def test_none_and_empty_body_render_nothing(self) -> None:
        assert build_snippet(None) is None
        assert build_snippet("") is None
        assert build_snippet("   ") is None

    def test_truncates_to_the_limit_after_cleaning(self) -> None:
        body = "Von: someone\n" + ("word " * 40)
        result = build_snippet(body, limit=20)
        assert result is not None
        assert len(result) <= 20

    def test_multiple_kept_lines_are_joined_with_spaces(self) -> None:
        body = "First line\nSecond line"
        assert build_snippet(body) == "First line Second line"
