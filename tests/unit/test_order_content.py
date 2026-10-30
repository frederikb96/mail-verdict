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


# A synthetic body of the shape a carrier template produces: the link text
# is an unfilled merge field and the shipment number exists only in the
# link target's query string.
_PICKUP_NOTICE = (
    "Hello,\n"
    "Your shipment [{DELIVERY_PARCEL_IDENTCODE}]"
    "(https://track.example.com/app/track?lang=en&piececode=00340000000000000001) "
    "is waiting at the pickup station.\n"
    + "Opening hours apply. " * 12
)


def test_a_shipment_number_only_in_a_link_target_reaches_the_text() -> None:
    prepared = prepare_body(body_text=_PICKUP_NOTICE, body_html=None)
    assert "00340000000000000001" in prepared.text
    assert "{DELIVERY_PARCEL_IDENTCODE}" not in prepared.text
    assert "https://" not in prepared.text


def test_a_link_with_text_gets_its_shipment_number_appended() -> None:
    prepared = prepare_body(
        body_text="Track it [here](https://track.example.com/t?tracking_number=JJD0001234567)",
        body_html=None,
    )
    assert prepared.text == "Track it here (JJD0001234567)"


def test_a_link_whose_text_already_shows_the_number_is_not_doubled() -> None:
    prepared = prepare_body(
        body_text="[JJD0001234567](https://track.example.com/t?tracking_number=JJD0001234567)",
        body_html=None,
    )
    assert prepared.text == "JJD0001234567"


def test_a_link_parameter_that_is_not_a_shipment_number_is_ignored() -> None:
    prepared = prepare_body(
        body_text="[Unsubscribe](https://shop.example.com/u?token=abcdef123456&lang=en)",
        body_html=None,
    )
    assert prepared.text == "Unsubscribe"


# A synthetic mail of the shape that used to read as a stub: a plain part that
# only says the client lacks HTML support (longer than the thin-text limit),
# and the whole order as table rows in the HTML part.
_STUB = (
    "Dear traveler,\n\nYour email program does not support HTML email. Follow the link "
    "below to view this message in your browser, where the order overview is shown.\n\n"
    "https://mail.example.com/view/abc123\n\nYours sincerely,\nExample Railways"
)
_ORDER_CARD = """
<html><body><table>
<tr><td colspan="2"><h2>Order overview</h2></td></tr>
<tr><td>Route</td><td>Springfield &rarr; Shelbyville</td></tr>
<tr><td>Travel date</td><td>Saturday 31 October 2026</td></tr>
<tr><td>Order number</td><td>E000555111</td></tr>
<tr><td>Total</td><td>EUR&nbsp;19.98</td></tr>
</table>
<p>Collect your tickets before travelling. This message is not a valid ticket and
carries no details beyond what the overview above shows. Keep the order number
for any question to customer service, which answers day and night on every day
of the week including public holidays.</p>
<p>How to pick up e-tickets: use the button below to go to My tickets, choose the
e-ticket you want to load in the app on your phone or print it from your computer.
Take a valid proof of identity with you when you travel, and arrive in good time
before departure so the tickets can be collected at the machine.</p>
</body></html>
"""


def test_a_plain_text_stub_loses_to_the_html_order_card() -> None:
    assert len(_STUB) > 200
    prepared = prepare_body(body_text=_STUB, body_html=_ORDER_CARD)
    assert "Order number | E000555111" in prepared.text
    assert "Travel date | Saturday 31 October 2026" in prepared.text
    assert "does not support HTML" not in prepared.text


def test_a_plain_part_that_is_html_source_is_converted() -> None:
    source = (
        "<!doctype html><html><head><style>.a { color: red; } .b { margin: 0 }</style></head>"
        "<body><table><tr><td>Order number</td><td>9292</td></tr></table></body></html>"
    )
    prepared = prepare_body(body_text=source, body_html=None)
    assert prepared.text == "Order number | 9292"
    assert "color" not in prepared.text


def test_a_dump_in_the_plain_part_is_replaced_by_the_html_part() -> None:
    dump = "<html><body>" + "<div style='x'>filler</div>" * 40 + "</body></html>"
    prepared = prepare_body(body_text=dump, body_html="<p>Your booking E1 is confirmed</p>")
    assert prepared.text == "Your booking E1 is confirmed"


def test_angle_bracket_links_in_plain_text_are_not_mistaken_for_markup() -> None:
    plain = "Track your parcel: " + " ".join(f"<https://t.example/{n}>" for n in range(30))
    prepared = prepare_body(body_text=plain, body_html="<p>different content</p>")
    assert prepared.text == "Track your parcel:"


def test_html_wins_only_when_it_has_clearly_more_text() -> None:
    plain = "Your order has shipped. " * 20
    size = len(plain.strip())
    just_short = "<p>" + "w" * (2 * size + 299) + "</p>"
    clearly_more = "<p>" + "w" * (2 * size + 300) + "</p>"
    assert prepare_body(body_text=plain, body_html=just_short).text == plain.strip()
    assert prepare_body(body_text=plain, body_html=clearly_more).text.startswith("www")


def test_html_entities_are_decoded() -> None:
    prepared = prepare_body(
        body_text=None, body_html="<p>Fish &amp; chips&nbsp;for &euro;5 &lt;eat in&gt;</p>",
    )
    assert prepared.text == "Fish & chips for €5 <eat in>"


def test_table_cells_become_label_value_pairs_and_rows_become_lines() -> None:
    prepared = prepare_body(
        body_text=None,
        body_html="<table><tr><td>Order date</td><td>29 Oct</td></tr>"
        "<tr><td>Order number</td><td>E55</td></tr></table>",
    )
    assert prepared.text == "Order date | 29 Oct\nOrder number | E55"


def test_a_shipment_number_in_an_html_link_target_reaches_the_text() -> None:
    prepared = prepare_body(
        body_text=None,
        body_html='<p>Your parcel <a href="https://track.example.com/t?piececode=00340000000000000007">'
        "{DELIVERY_PARCEL_IDENTCODE}</a> is on its way.</p>",
    )
    assert "00340000000000000007" in prepared.text
    assert "https://" not in prepared.text


def test_a_long_body_is_cut_to_head_and_tail_with_a_marker() -> None:
    body = "Order E-1 confirmed. " + "filler sentence. " * 1_500 + "\nTicketcode 7K2Q9"
    prepared = prepare_body(body_text=body, body_html=None)
    assert len(prepared.text) < 12_100
    assert prepared.text.startswith("Order E-1 confirmed.")
    assert prepared.text.endswith("Ticketcode 7K2Q9")
    assert "characters omitted ...]" in prepared.text
    assert body.strip() == prepared.raw.strip(), "the raw source stays whole for number search"


def test_a_body_at_the_limit_is_kept_whole() -> None:
    body = ("x" * 99 + " ") * 120
    prepared = prepare_body(body_text=body, body_html=None)
    assert prepared.text == body.strip()
