"""
orders/content.py's prepare_body: markdown links keep their label, bare
and bracketed URLs vanish, a body of mostly links shrinks to its text,
and HTML is used when the plain text is too short to be the real content.
"""

from __future__ import annotations

from mail_verdict.orders.content import prepare_body


def test_a_markdown_link_keeps_its_label() -> None:
    prepared = prepare_body(body_text="Track your parcel [here](https://dhl.de/x)", body_html=None)
    assert prepared.text == "Track your parcel here"


def test_a_bracketed_url_vanishes() -> None:
    prepared = prepare_body(body_text="See [https://example.com/track] for status", body_html=None)
    assert "https://example.com" not in prepared.text


def test_an_angle_bracket_url_vanishes() -> None:
    prepared = prepare_body(body_text="Visit <https://example.com/x> now", body_html=None)
    assert "https://example.com" not in prepared.text


def test_a_bare_url_vanishes() -> None:
    prepared = prepare_body(body_text="Go to https://example.com/x today", body_html=None)
    assert "https://example.com" not in prepared.text
    assert "Go to" in prepared.text
    assert "today" in prepared.text


def test_image_markers_are_dropped() -> None:
    prepared = prepare_body(body_text="[image: logo.png] Your order shipped", body_html=None)
    assert "image" not in prepared.text.lower()
    assert "Your order shipped" in prepared.text


def test_a_body_of_mostly_links_shrinks_to_its_text() -> None:
    """The DHL case observed during design: a first chunk that is almost
    entirely language-switcher links shrinks to a fraction of its
    original length once link targets are dropped."""
    links = " ".join(f"[Deutsch](https://dhl.de/lang/{i})" for i in range(30))
    body = f"{links}\nYour parcel 123456 is on its way."
    prepared = prepare_body(body_text=body, body_html=None)
    assert len(prepared.text) < len(body) / 2
    assert "Your parcel 123456 is on its way." in prepared.text


def test_html_is_used_when_plain_text_is_too_short() -> None:
    prepared = prepare_body(
        body_text="ok", body_html="<p>Your order <b>12345</b> has shipped</p>",
    )
    assert "12345" in prepared.text
    assert "has shipped" in prepared.text


def test_plain_text_is_used_when_long_enough_even_with_html_present() -> None:
    long_text = "Your order has shipped. " * 10
    prepared = prepare_body(body_text=long_text, body_html="<p>different content</p>")
    assert "different content" not in prepared.text
    assert "Your order has shipped." in prepared.text


def test_raw_carries_body_text_and_body_html_untouched() -> None:
    prepared = prepare_body(body_text="see https://x.example/y", body_html="<p>hi</p>")
    assert "https://x.example/y" in prepared.raw
    assert "<p>hi</p>" in prepared.raw


def test_three_or_more_newlines_collapse_to_two() -> None:
    prepared = prepare_body(body_text="one\n\n\n\ntwo", body_html=None)
    assert prepared.text == "one\n\ntwo"


def test_a_nonbreaking_space_collapses_like_an_ordinary_space() -> None:
    prepared = prepare_body(body_text="order number 123", body_html=None)
    assert prepared.text == "order number 123"
