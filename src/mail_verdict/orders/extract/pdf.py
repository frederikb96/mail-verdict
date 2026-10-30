"""
PDF reading: the text layer through PDFium, barcodes from page renders, and
Tesseract for pages that have no text layer.

A barcode is placed into the page text by comparing its position with each
glyph's: the marker goes before the line of the first glyph that follows the
code in reading order. The text itself stays in PDFium's own order.
"""

from __future__ import annotations

import pypdfium2 as pdfium
from PIL import Image

from mail_verdict.config.loader import ExtractionConfig
from mail_verdict.orders.extract import barcodes as bc
from mail_verdict.orders.extract.model import Read
from mail_verdict.orders.extract.ocr import OcrResult, ocr_image

# A page with fewer text characters than this is a scan or an outlined-font
# page and is read by OCR instead.
OCR_MIN_TEXT_CHARS = 50
OCR_DPI = 200
_CROP_SCALE_TARGET = 500
_CROP_PAD_POINTS = 6.0


def _scale_for(page: pdfium.PdfPage, dpi: int) -> float:
    width, height = page.get_size()
    return float(min(dpi / 72, bc.MAX_RENDER_SIDE / max(width, height, 1)))


def _render(page: pdfium.PdfPage, dpi: int) -> tuple[Image.Image, float]:
    scale = _scale_for(page, dpi)
    return page.render(scale=scale).to_pil(), scale


def _code_crop(page: pdfium.PdfPage, bbox: tuple[float, float, float, float]) -> bytes:
    """A sharp PNG of one code, rendered from the page at a size that suits it."""
    width, height = page.get_size()
    left = max(0.0, bbox[0] - _CROP_PAD_POINTS)
    top = max(0.0, bbox[1] - _CROP_PAD_POINTS)
    right = min(width, bbox[2] + _CROP_PAD_POINTS)
    bottom = min(height, bbox[3] + _CROP_PAD_POINTS)
    scale = min(8.0, max(2.0, _CROP_SCALE_TARGET / max(right - left, bottom - top, 1.0)))
    image = page.render(
        scale=scale, crop=(left, height - bottom, width - right, top),
    ).to_pil()
    return bc.crop_png(image, (0, 0, image.width, image.height))


def _glyph_anchors(
    page: pdfium.PdfPage, textpage: pdfium.PdfTextPage, text: str,
) -> list[tuple[int, float, float]]:
    """(index, top, left) in points from the page's top-left, for every glyph."""
    height = page.get_size()[1]
    anchors = []
    for index, char in enumerate(text):
        if char.isspace():
            continue
        left, _bottom, _right, top = textpage.get_charbox(index)
        anchors.append((index, height - top, left))
    return anchors


def read_pdf(data: bytes, limits: ExtractionConfig) -> Read:
    """
    Read a PDF into text and codes.

    Args:
        data: The file
        limits: Page and OCR limits (config.yaml, `extraction`)

    Returns:
        Method `ocr` when any page needed Tesseract, else `text_layer`

    Raises:
        pdfium.PdfiumError: The file is not a readable PDF (corrupt, encrypted)
        mail_verdict.orders.extract.ocr.OcrError: A page needed OCR and it failed
    """
    pdf = pdfium.PdfDocument(data)
    try:
        total = len(pdf)
        book = bc.CodeBook()
        pages: list[str] = []
        ocr_used = 0
        for number in range(min(total, limits.max_pdf_pages)):
            page = pdf[number]
            textpage = page.get_textpage()
            text = textpage.get_text_range()
            anchors: list[tuple[int, float, float]] | None = None
            ocr_result: OcrResult | None = None

            if len(text.strip()) < OCR_MIN_TEXT_CHARS and ocr_used < limits.ocr_pages:
                ocr_used += 1
                image, render_scale = _render(page, OCR_DPI)
                ocr_result = ocr_image(
                    image, languages=limits.ocr_languages, timeout=limits.ocr_timeout_seconds,
                )
                text = ocr_result.text
                anchors = None if ocr_result.rotated else ocr_result.anchors(render_scale)

            placed: list[tuple[str, float, float]] = []
            if number < limits.barcode_pages:
                image, scale = _render(page, bc.SCAN_DPI)
                found = bc.scan(image)
                if not found and number < bc.RETRY_PAGES:
                    image, scale = _render(page, bc.RETRY_DPI)
                    found = bc.scan(image)
                for raw in found:
                    bbox = tuple(v / scale for v in raw.box)
                    code = book.add(
                        raw, page=number + 1, bbox=bbox,  # type: ignore[arg-type]
                        crop=_code_crop(page, bbox),  # type: ignore[arg-type]
                    )
                    if code is not None:
                        placed.append((
                            bc.marker(code.index, code.format, code.payload, code.text),
                            bbox[1], bbox[0],
                        ))
            if placed:
                if anchors is None and ocr_result is None:
                    anchors = _glyph_anchors(page, textpage, text)
                text = bc.insert_markers(text, anchors or [], placed)
            pages.append(text.replace("\r\n", "\n").strip())
        joined = "\n\n".join(p for p in pages if p)
        return Read(
            method="ocr" if ocr_used else "text_layer", text=joined, page_count=total,
            barcodes=book.with_offsets(joined),
        )
    finally:
        pdf.close()
