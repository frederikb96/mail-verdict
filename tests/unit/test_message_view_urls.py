"""
A message's URLs are the strongest spam signal it carries, so they must
reach the classify stage regardless of where in the body they fell or
whether the body is HTML -- see message_view.py's module docstring.
"""

from __future__ import annotations

from mail_verdict.pipeline.message_view import (
    _BODY_EXCERPT_CHARS,
    _append_missing_urls,
    _extract_urls,
)


class TestExtractUrls:
    def test_bare_url_in_plain_text(self) -> None:
        urls = _extract_urls(body_text="Check this out: https://example.com/offer", body_html=None)
        assert urls == ("https://example.com/offer",)

    def test_href_target_hidden_behind_anchor_text(self) -> None:
        """An anchor's real target must reach the model even when its
        visible text says nothing about a link -- nh3.clean(tags=set())
        discards the href along with the rest of the markup."""
        html = '<a href="https://phish.example/verify">Click here to verify your account</a>'
        urls = _extract_urls(body_text=None, body_html=html)
        assert urls == ("https://phish.example/verify",)

    def test_bare_url_typed_directly_into_html(self) -> None:
        html = "<p>See https://example.com/promo for details</p>"
        urls = _extract_urls(body_text=None, body_html=html)
        assert urls == ("https://example.com/promo",)

    def test_no_urls_returns_empty(self) -> None:
        assert _extract_urls(body_text="nothing interesting here", body_html=None) == ()

    def test_deduplicates_preserving_first_seen_order(self) -> None:
        text = "https://a.example.com then https://b.example.com then https://a.example.com again"
        assert _extract_urls(body_text=text, body_html=None) == (
            "https://a.example.com", "https://b.example.com",
        )

    def test_trailing_sentence_punctuation_is_stripped(self) -> None:
        urls = _extract_urls(body_text="Visit https://example.com/x.", body_html=None)
        assert urls == ("https://example.com/x",)

    def test_url_in_parentheses_is_stripped_of_the_closing_paren(self) -> None:
        urls = _extract_urls(body_text="(see https://example.com/x)", body_html=None)
        assert urls == ("https://example.com/x",)

    def test_capped_at_max_urls(self) -> None:
        text = " ".join(f"https://example.com/{i}" for i in range(50))
        urls = _extract_urls(body_text=text, body_html=None)
        assert len(urls) == 20

    def test_non_http_href_is_ignored(self) -> None:
        """A mailto: or javascript: target is not a spam-signal URL."""
        html = '<a href="mailto:someone@example.com">Email us</a>'
        assert _extract_urls(body_text=None, body_html=html) == ()


class TestAppendMissingUrls:
    def test_no_urls_leaves_excerpt_unchanged(self) -> None:
        assert _append_missing_urls("plain body", ()) == "plain body"

    def test_url_already_visible_in_excerpt_is_not_repeated(self) -> None:
        excerpt = "Visit https://example.com/x for details"
        result = _append_missing_urls(excerpt, ("https://example.com/x",))
        assert result == excerpt
        assert result.count("https://example.com/x") == 1

    def test_url_missing_from_excerpt_is_appended(self) -> None:
        result = _append_missing_urls("short body", ("https://example.com/hidden",))
        assert "short body" in result
        assert "https://example.com/hidden" in result

    def test_a_url_past_the_truncation_cut_is_still_appended(self) -> None:
        """The realistic case this whole mechanism exists for: a long
        body's prefix excerpt drops a URL that sat near the end."""
        long_body = ("filler " * 2000) + "https://example.com/late-link"
        excerpt = long_body[:_BODY_EXCERPT_CHARS]
        assert "https://example.com/late-link" not in excerpt  # sanity: the cut really drops it

        result = _append_missing_urls(excerpt, ("https://example.com/late-link",))
        assert "https://example.com/late-link" in result

    def test_result_never_exceeds_the_body_excerpt_budget(self) -> None:
        long_body = "x" * _BODY_EXCERPT_CHARS
        many_urls = tuple(f"https://example.com/{i}" for i in range(20))
        result = _append_missing_urls(long_body, many_urls)
        assert len(result) <= _BODY_EXCERPT_CHARS

    def test_appended_urls_are_never_truncated_mid_string(self) -> None:
        """A partial URL is worse than an absent one -- the excerpt's own
        tail gives way, never the URL list."""
        long_body = "x" * _BODY_EXCERPT_CHARS
        urls = tuple(f"https://example.com/path-{i}-with-some-length-to-it" for i in range(20))
        result = _append_missing_urls(long_body, urls)
        for url in urls:
            assert url in result
