"""
orders/extract: PDF text layers, page and size limits, and the dispatch that
turns an unreadable file into a `failed` document instead of an exception.
"""

from __future__ import annotations

import pytest

from mail_verdict.orders.extract.reader import extract_bytes
from tests.helpers.documents import limits, make_pdf

_LINES = [
    (72, 760, "Online-Ticket"),
    (72, 740, "Order number: 258285260829"),
    (72, 720, "Total price 77,55 EUR (incl. VAT)"),
]


def test_a_text_layer_is_read_in_order() -> None:
    doc = extract_bytes(make_pdf(_LINES), "application/pdf", "ticket.pdf", limits())
    assert doc.method == "text_layer"
    assert doc.page_count == 1
    assert doc.text.splitlines() == [
        "Online-Ticket", "Order number: 258285260829", "Total price 77,55 EUR (incl. VAT)",
    ]
    assert doc.text_chars_total == len(doc.text)
    assert doc.barcodes == ()


def test_the_file_is_identified_by_content_not_by_its_label() -> None:
    doc = extract_bytes(make_pdf(_LINES), "application/octet-stream", "download", limits())
    assert doc.method == "text_layer"
    assert "Online-Ticket" in doc.text


def test_pages_are_joined_and_capped_by_the_page_limit() -> None:
    data = make_pdf([(72, 760, "Page marker")], pages=5)
    doc = extract_bytes(data, "application/pdf", "five.pdf", limits(max_pdf_pages=2))
    assert doc.page_count == 5
    assert doc.text.count("Page marker") == 2


def test_text_beyond_the_ceiling_is_cut_but_counted() -> None:
    data = make_pdf([(72, 760, "x" * 90)] + [(72, 760 - 12 * n, "y" * 90) for n in range(1, 40)])
    doc = extract_bytes(data, "application/pdf", "long.pdf", limits(max_text_chars=500))
    assert len(doc.text) == 500
    assert doc.text_chars_total > 500


def test_a_corrupt_pdf_comes_back_failed_with_the_reason() -> None:
    doc = extract_bytes(b"%PDF-1.4\nthis is not a pdf", "application/pdf", "bad.pdf", limits())
    assert doc.method == "failed"
    assert doc.error
    assert doc.text == ""
    assert doc.size_bytes == len(b"%PDF-1.4\nthis is not a pdf")


def test_a_file_over_the_size_limit_is_failed_unread() -> None:
    data = make_pdf(_LINES)
    doc = extract_bytes(data, "application/pdf", "big.pdf", limits(max_document_bytes=100))
    assert doc.method == "failed"
    assert "larger than 100 bytes" in (doc.error or "")


@pytest.mark.parametrize("data", [b"", b"\x00\x01\x02 unknown binary"])
def test_an_unknown_file_is_none_not_an_error(data: bytes) -> None:
    doc = extract_bytes(data, "application/x-thing", "thing.bin", limits())
    assert doc.method == "none"
    assert doc.error is None
    assert len(doc.sha256) == 64
