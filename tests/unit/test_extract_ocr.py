"""
orders/extract OCR: Tesseract for pages and images with no text layer. These
tests need the `tesseract` binary with every language in config.yaml's
`extraction.ocr_languages` (apt: tesseract-ocr, tesseract-ocr-deu,
tesseract-ocr-nld); without them they fail, they do not skip, because a
skipped OCR test is how a deployment without OCR goes unnoticed.
"""

from __future__ import annotations

import io
import shutil
import subprocess

from mail_verdict.orders.extract.reader import extract_bytes
from tests.helpers.documents import (
    barcode_image,
    limits,
    make_pdf,
    png,
    scanned_pdf,
    text_image,
)

_LINES = [
    "Rechnung Nr. 2030-4711",
    "Betrag 232,13 EUR faellig am 16.09.2030",
    "Groesse: Small, Strasse: Schindlerweg",
]


def test_tesseract_and_every_configured_language_are_installed() -> None:
    binary = shutil.which("tesseract")
    assert binary, "tesseract is not on PATH (apt install tesseract-ocr)"
    listed = subprocess.run(
        [binary, "--list-langs"], capture_output=True, text=True, check=True,
    ).stdout.split()
    missing = [lang for lang in limits().ocr_languages.split("+") if lang not in listed]
    assert not missing, f"tesseract languages missing: {missing} (apt install tesseract-ocr-<lang>)"


def test_a_page_without_a_text_layer_is_read_by_ocr() -> None:
    doc = extract_bytes(scanned_pdf(text_image(_LINES)), "application/pdf", "scan.pdf", limits())
    assert doc.method == "ocr", doc.error
    assert "2030-4711" in doc.text
    assert "232,13" in doc.text
    assert "16.09.2030" in doc.text
    assert "Schindlerweg" in doc.text


def test_an_upside_down_page_is_turned_and_read() -> None:
    upside_down = text_image(_LINES).rotate(180)
    doc = extract_bytes(scanned_pdf(upside_down), "application/pdf", "scan.pdf", limits())
    assert doc.method == "ocr", doc.error
    assert "232,13" in doc.text


def test_a_page_with_a_text_layer_is_not_sent_to_ocr() -> None:
    data = make_pdf([(72, 780 - 14 * n, f"Line number {n} of a normal document") for n in range(6)])
    assert extract_bytes(data, "application/pdf", "t.pdf", limits()).method == "text_layer"


def test_only_the_configured_number_of_pages_is_sent_to_ocr() -> None:
    page = text_image(_LINES)
    out = io.BytesIO()
    page.convert("RGB").save(
        out, format="PDF", resolution=150.0, save_all=True, append_images=[page, page],
    )
    doc = extract_bytes(out.getvalue(), "application/pdf", "three.pdf", limits(ocr_pages=1))
    assert doc.page_count == 3
    assert doc.text.count("2030-4711") == 1


def test_a_large_image_is_read_by_ocr_and_its_code_is_placed_in_the_text() -> None:
    page = text_image(_LINES + ["", "", "", "Scan below"], width=1500)
    code = barcode_image("SCAN-QR-1", "QRCode", scale=5)
    page.paste(code, (900, page.height - code.height - 40))
    doc = extract_bytes(png(page), "image/png", "photo.png", limits())
    assert doc.method == "image"
    assert "232,13" in doc.text
    assert [c.text for c in doc.barcodes] == ["SCAN-QR-1"]
    assert doc.barcodes[0].text_offset is not None


def test_a_logo_sized_image_is_not_sent_to_ocr() -> None:
    small = text_image(["Logo"], size=20, width=300)
    doc = extract_bytes(png(small), "image/png", "logo.png", limits())
    assert doc.method == "image"
    assert doc.text == ""
