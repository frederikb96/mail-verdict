"""
orders/extract: the non-PDF document types -- calendar files, e-invoices,
HTML attachments, plain text -- and how the reader picks one.
"""

from __future__ import annotations

from mail_verdict.orders.extract.reader import extract_bytes
from tests.helpers.documents import FIXTURES, limits

_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example//EN
METHOD:PUBLISH
BEGIN:VEVENT
UID:abc-123@example.com
SUMMARY:Station A to Station B
DTSTART;TZID=Europe/Berlin:20300102T093000
DTEND;TZID=Europe/Berlin:20300102T151500
LOCATION:Station A
ORGANIZER:mailto:booking@example.com
DESCRIPTION:Second class\\nTrain 785 platform 4
END:VEVENT
END:VCALENDAR
"""


def test_a_calendar_file_gives_one_block_per_event() -> None:
    doc = extract_bytes(_ICS, "text/calendar", "trip.ics", limits())
    assert doc.method == "ics"
    assert doc.text.splitlines()[0] == "Event: Station A to Station B"
    assert "Start: 2030-01-02T09:30:00+01:00" in doc.text
    assert "End: 2030-01-02T15:15:00+01:00" in doc.text
    assert "Location: Station A" in doc.text
    assert "Organizer: booking@example.com" in doc.text
    assert "Train 785 platform 4" in doc.text


def test_a_calendar_with_no_events_is_empty_not_failed() -> None:
    data = b"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:x\nEND:VCALENDAR\n"
    doc = extract_bytes(data, "text/calendar", "empty.ics", limits())
    assert (doc.method, doc.text, doc.error) == ("ics", "", None)


def test_a_ubl_invoice_gives_its_key_fields() -> None:
    data = (FIXTURES / "invoice_ubl.xml").read_bytes()
    doc = extract_bytes(data, "text/xml", "invoice.xml", limits())
    assert doc.method == "xml"
    assert doc.text.splitlines() == [
        "Invoice number: INV-2030-0042",
        "Issue date: 2030-03-01",
        "Due date: 2030-03-15",
        "Order reference: ORD-778",
        "Issuer: Example Textiles Ltd",
        "Customer: Sample Customer",
        "Total: 20.00 EUR",
        "Still payable: 0.00 EUR",
        "Tax: 3.20 EUR",
        "IBAN: DE02120300000000202051",
        "Note: Online order ORD-778 of 2030-02-27",
    ]


def test_xml_that_is_not_an_invoice_is_not_read() -> None:
    feed = b"<?xml version='1.0'?><feed><item>x</item></feed>"
    doc = extract_bytes(feed, "text/xml", "f.xml", limits())
    assert (doc.method, doc.text) == ("none", "")


def test_xml_with_entity_declarations_is_refused_unparsed() -> None:
    bomb = b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa">]><Invoice>&a;</Invoice>'
    doc = extract_bytes(bomb, "text/xml", "bomb.xml", limits())
    assert (doc.method, doc.text) == ("none", "")


def test_malformed_xml_is_failed() -> None:
    doc = extract_bytes(b"<Invoice><ID>1</Invoice", "text/xml", "bad.xml", limits())
    assert doc.method == "failed"


def test_an_html_attachment_keeps_its_table_rows_and_drops_scripts() -> None:
    data = (FIXTURES / "order_table.html").read_bytes()
    doc = extract_bytes(data, "text/html", "confirmation.html", limits())
    assert doc.method == "html"
    assert "Order number | E000111222" in doc.text
    assert "Total | EUR 19,98 & fees" in doc.text
    assert "29 Oct 2026" in doc.text
    assert "ignored" not in doc.text
    assert "color: red" not in doc.text


def test_plain_text_is_read_as_is() -> None:
    doc = extract_bytes("Zahlung über 12,50 €\n".encode(), "text/plain", "note.txt", limits())
    assert (doc.method, doc.text) == ("text_layer", "Zahlung über 12,50 €")


def test_a_latin1_text_file_is_decoded() -> None:
    doc = extract_bytes("Größe 42".encode("latin-1"), "text/plain", "note.txt", limits())
    assert doc.text == "Größe 42"


def test_an_unreadable_image_is_failed() -> None:
    doc = extract_bytes(b"\x89PNG\r\n\x1a\nnot really", "image/png", "broken.png", limits())
    assert doc.method == "failed"
    assert doc.error


def test_an_image_over_the_pixel_limit_is_refused_before_decoding() -> None:
    from PIL import Image

    from tests.helpers.documents import png

    data = png(Image.new("L", (300, 300), 255))
    doc = extract_bytes(data, "image/png", "wide.png", limits(max_image_pixels=1000))
    assert doc.method == "failed"
    assert doc.error


def test_a_file_no_reader_knows_is_stored_unread_not_failed() -> None:
    for data, ctype, name in (
        (b"PK\x03\x04\x14\x00\x06\x00 word/document.xml", "application/octet-stream", "a.docx"),
        (b"\x00\x00\x00\x18ftypheic\x00\x00", "image/heic", "photo.heic"),
        (b"\x00\x00\x00\x18ftypheic\x00\x00", "application/octet-stream", "photo.heic"),
        (b"RIFF\x24\x00\x00\x00WAVEfmt ", "application/octet-stream", "clip.wav"),
    ):
        doc = extract_bytes(data, ctype, name, limits())
        assert (doc.method, doc.text, doc.error) == ("none", "", None), name
        assert doc.sha256


def test_text_that_starts_like_a_bitmap_is_still_text() -> None:
    data = b"BM is not enough to call a text an image"
    doc = extract_bytes(data, "text/plain", "m.txt", limits())
    assert doc.method == "text_layer"


def test_webp_gif_and_tiff_go_through_the_image_reader() -> None:
    import io

    from PIL import Image

    for fmt, ctype in (
        ("WEBP", "application/octet-stream"), ("GIF", "application/octet-stream"),
        ("TIFF", "application/octet-stream"), ("BMP", "image/bmp"),
    ):
        buffer = io.BytesIO()
        Image.new("L", (40, 40), 255).save(buffer, format=fmt)
        doc = extract_bytes(buffer.getvalue(), ctype, "scan", limits())
        assert doc.method == "image", fmt
