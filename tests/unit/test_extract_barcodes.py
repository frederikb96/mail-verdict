"""
orders/extract barcodes: QR, Aztec and 1D codes decoded from PDF pages, images
and passes, with the exact payload bytes, the page position and the marker's
place in the document text.
"""

from __future__ import annotations

import json

from PIL import Image

from mail_verdict.orders.extract import barcodes as bc
from mail_verdict.orders.extract.model import CODE_MARKER_RE
from mail_verdict.orders.extract.reader import extract_bytes
from tests.helpers.documents import barcode_image, limits, make_pdf, make_pkpass, png

# An Aztec ticket payload is binary: every byte value, plus a header.
_BINARY = b"#UT02" + bytes(range(256)) + b"\x00\xff\x00"


def _ticket_pdf(code: Image.Image, *, x: float = 380, y: float = 600) -> bytes:
    """Three text lines at the top and three at the bottom, the code in between."""
    return make_pdf(
        [
            (72, 780, "Header line one"), (72, 764, "Header line two"),
            (72, 748, "Header line three"),
            (72, 300, "Footer line one"), (72, 284, "Footer line two"),
        ],
        [(x, y, 130, code)],
    )


def test_an_aztec_code_is_decoded_to_its_exact_bytes() -> None:
    pdf = _ticket_pdf(barcode_image(_BINARY, "Aztec"))
    doc = extract_bytes(pdf, "application/pdf", "ticket.pdf", limits())
    assert [b.format for b in doc.barcodes] == ["Aztec"]
    code = doc.barcodes[0]
    assert code.payload == _BINARY
    assert code.text is None
    assert code.page == 1


def test_the_marker_sits_between_the_text_above_and_below_the_code() -> None:
    pdf = _ticket_pdf(barcode_image(_BINARY, "Aztec"), x=72, y=500)
    doc = extract_bytes(pdf, "application/pdf", "ticket.pdf", limits())
    marker = CODE_MARKER_RE.search(doc.text)
    assert marker is not None
    assert marker.group() == f"[code b1 Aztec {len(_BINARY)} bytes]"
    above = doc.text.index("Header line three")
    below = doc.text.index("Footer line one")
    assert above < marker.start() < below
    assert doc.barcodes[0].text_offset == marker.start()


def test_a_code_at_the_top_is_placed_before_the_text_below_it() -> None:
    pdf = _ticket_pdf(barcode_image("TOP-CODE-123", "QRCode"), x=380, y=700)
    doc = extract_bytes(pdf, "application/pdf", "ticket.pdf", limits())
    assert doc.text.startswith('[code b1 QR "TOP-CODE-123"]\n')


def test_a_position_in_points_is_reported_from_the_top_left() -> None:
    pdf = _ticket_pdf(barcode_image("POS-1", "QRCode"), x=380, y=600)
    code = extract_bytes(pdf, "application/pdf", "ticket.pdf", limits()).barcodes[0]
    assert code.bbox is not None
    left, top, right, bottom = code.bbox
    # Placed at x 380..510 and 600..730 from the bottom of an 842 pt page; the
    # quiet zone around the modules shrinks the decoded box inside that.
    assert 380 <= left < right <= 510
    assert 842 - 730 <= top < bottom <= 842 - 600


def test_a_printable_payload_is_kept_as_text_and_shown_in_the_marker() -> None:
    pdf = _ticket_pdf(barcode_image("5471205382", "QRCode"))
    doc = extract_bytes(pdf, "application/pdf", "ticket.pdf", limits())
    assert doc.barcodes[0].text == "5471205382"
    assert '[code b1 QR "5471205382"]' in doc.text


def test_a_long_payload_is_summarised_by_its_size() -> None:
    text = "https://tickets.example/t/" + "a1b2c3" * 20
    doc = extract_bytes(_ticket_pdf(barcode_image(text, "QRCode")), "", "t.pdf", limits())
    assert doc.barcodes[0].text == text
    assert f"[code b1 QR {len(text)} bytes]" in doc.text


def test_one_dimensional_codes_are_read_too() -> None:
    image = barcode_image("ABC-123456", "Code128", scale=3)
    doc = extract_bytes(
        make_pdf([(72, 780, "Label")], [(72, 600, 200, image.resize((300, 120)))]),
        "", "label.pdf", limits(),
    )
    assert [(b.format, b.text) for b in doc.barcodes] == [("Code128", "ABC-123456")]


def test_two_different_codes_are_numbered_in_reading_order() -> None:
    pdf = make_pdf(
        [(72, 780, "Heading")],
        [
            (72, 650, 120, barcode_image("FIRST", "QRCode")),
            (72, 450, 120, barcode_image("SECOND", "QRCode")),
        ],
    )
    codes = extract_bytes(pdf, "", "two.pdf", limits()).barcodes
    assert sorted(c.text or "" for c in codes) == ["FIRST", "SECOND"]
    assert [c.index for c in codes] == [1, 2]


def test_the_same_code_printed_twice_is_one_code() -> None:
    image = barcode_image("SAME", "QRCode")
    pdf = make_pdf([(72, 780, "Twice")], [(72, 650, 120, image), (300, 450, 120, image)])
    doc = extract_bytes(pdf, "", "dup.pdf", limits())
    assert len(doc.barcodes) == 1
    assert doc.text.count("[code ") == 1


def test_only_the_leading_pages_are_scanned() -> None:
    pdf = make_pdf([(72, 780, "Page")], [(72, 500, 120, barcode_image("P1", "QRCode"))], pages=1)
    assert extract_bytes(pdf, "", "a.pdf", limits(barcode_pages=1)).barcodes
    assert not extract_bytes(pdf, "", "a.pdf", limits(barcode_pages=0)).barcodes


def test_a_code_in_an_image_attachment_is_decoded() -> None:
    data = png(barcode_image("RETURN-LABEL-77", "QRCode", scale=6))
    doc = extract_bytes(data, "image/png", "label.png", limits())
    assert doc.method == "image"
    assert [b.text for b in doc.barcodes] == ["RETURN-LABEL-77"]
    assert doc.barcodes[0].page == 1
    assert doc.text.strip() == '[code b1 QR "RETURN-LABEL-77"]'


def test_a_logo_sized_image_is_not_scanned() -> None:
    tiny = barcode_image("X", "QRCode", scale=2).resize((60, 60))
    doc = extract_bytes(png(tiny), "image/png", "logo.png", limits())
    assert doc.method == "image"
    assert doc.barcodes == ()


def test_a_wallet_pass_carries_its_code_without_decoding() -> None:
    data = make_pkpass([
        {"format": "PKBarcodeFormatQR", "message": "5471205382", "messageEncoding": "iso-8859-1"},
    ])
    doc = extract_bytes(data, "application/vnd.apple.pkpass", "ticket.pkpass", limits())
    assert doc.method == "pkpass"
    assert [(b.format, b.text, b.page) for b in doc.barcodes] == [("QR", "5471205382", None)]
    assert doc.barcodes[0].text_offset == doc.text.index("[code b1 ")
    assert "Seat: Row 4, 7-8" in doc.text
    assert "Film: Example Film" in doc.text
    assert "Relevant date: 2030-05-04T18:30:00+02:00" in doc.text


def test_a_stored_payload_re_encodes_to_the_same_code() -> None:
    rendered = bc.render_code(_BINARY, "Aztec", None, scale=6)
    assert rendered is not None
    from io import BytesIO

    again = bc.scan(Image.open(BytesIO(rendered)))
    assert [(c.format, c.payload) for c in again] == [("Aztec", _BINARY)]


def test_a_format_without_a_writer_renders_nothing() -> None:
    assert bc.render_code(b"x", "NoSuchFormat", None, scale=4) is None


def test_the_stored_json_shape_carries_bytes_and_a_crop() -> None:
    pdf = _ticket_pdf(barcode_image(_BINARY, "Aztec"))
    stored = extract_bytes(pdf, "", "t.pdf", limits()).barcodes[0].to_json()
    json.dumps(stored)
    assert set(stored) == {
        "index", "format", "text", "bytes_b64", "page", "bbox", "text_offset", "crop_b64",
    }
    assert stored["crop_b64"]
