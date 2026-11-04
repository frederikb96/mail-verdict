"""
Image attachments and inline graphics: a barcode scan always (a DHL return
label is a PNG with a QR code), OCR only for images big enough to hold a
document rather than a logo. An image OCR reads poorly also yields a small
JPEG copy for the read model (`vision_below_confidence` in config.yaml).
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
    confidence = ocr_result.confidence if ocr_result else None
    vision_image = (
        vision_copy(image, limits.vision_max_side)
        if confidence is not None and confidence < limits.vision_below_confidence else None
    )
    return Read(
        method="image", text=text, page_count=1, barcodes=book.with_offsets(text),
        ocr_confidence=confidence, vision_image=vision_image,
    )


def vision_copy(image: Image.Image, max_side: int) -> bytes:
    """The image as a JPEG no longer than `max_side` on either side -- small
    enough for every vision API, large enough to read a ticket's fine print."""
    copy = image.convert("RGB")
    copy.thumbnail((max_side, max_side))
    buffer = io.BytesIO()
    copy.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()
