"""
Image attachments and inline graphics: a barcode scan always (a DHL return
label is a PNG with a QR code), OCR only for images big enough to hold a
document rather than a logo.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps

from mail_verdict.config.loader import ExtractionConfig
from mail_verdict.orders.extract import barcodes as bc
from mail_verdict.orders.extract.model import Read
from mail_verdict.orders.extract.ocr import ocr_image

# Logos, banners and template icons are smaller than this; a photographed or
# scanned document is not.
OCR_MIN_WIDTH = 600
OCR_MIN_HEIGHT = 400


def read_image(data: bytes, limits: ExtractionConfig) -> Read:
    """
    Read an image for codes and, when large enough, text.

    Args:
        data: The image file
        limits: Pixel and OCR limits (config.yaml, `extraction`)

    Returns:
        Method `image`

    Raises:
        PIL.UnidentifiedImageError, ValueError: Not a readable image, or too large
        mail_verdict.orders.extract.ocr.OcrError: OCR was due and failed
    """
    with Image.open(io.BytesIO(data)) as opened:
        if opened.width * opened.height > limits.max_image_pixels:
            raise ValueError(f"image has {opened.width * opened.height} pixels")
        image = ImageOps.exif_transpose(opened)
        image.load()
    text = ""
    ocr_result = None
    if image.width >= OCR_MIN_WIDTH and image.height >= OCR_MIN_HEIGHT:
        ocr_result = ocr_image(
            image, languages=limits.ocr_languages, timeout=limits.ocr_timeout_seconds,
        )
        text = ocr_result.text

    book = bc.CodeBook()
    placed: list[tuple[str, float, float]] = []
    if image.width >= bc.MIN_IMAGE_SIDE and image.height >= bc.MIN_IMAGE_SIDE:
        for raw in bc.scan(image):
            code = book.add(raw, page=1, bbox=raw.box, crop=bc.crop_png(image, raw.box))
            if code is not None:
                placed.append((
                    bc.marker(code.index, code.format, code.payload, code.text),
                    raw.box[1], raw.box[0],
                ))
    if placed:
        anchors = ocr_result.anchors() if ocr_result and not ocr_result.rotated else []
        text = bc.insert_markers(text, anchors, placed)
    return Read(method="image", text=text, page_count=1, barcodes=book.with_offsets(text))
