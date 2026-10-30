"""
Synthetic documents for the extraction tests, built in memory: a PDF with a
real text layer and placed images, a PDF that is only a picture of text, a
wallet pass, and barcode images from the same encoder the application uses.
Nothing here is a copy of a real document.
"""

from __future__ import annotations

import io
import json
import zipfile
import zlib
from collections.abc import Sequence
from pathlib import Path

import yaml
import zxingcpp
from PIL import Image, ImageDraw, ImageFont

from mail_verdict.config.loader import ExtractionConfig

FIXTURES = Path(__file__).parent.parent / "fixtures" / "documents"
_CONFIG = Path(__file__).parent.parent.parent / "config" / "config.yaml"


def limits(**overrides: object) -> ExtractionConfig:
    """The shipped `extraction` section, with any field replaced."""
    with open(_CONFIG) as handle:
        values = yaml.safe_load(handle)["extraction"]
    return ExtractionConfig(**{**values, **overrides})


def barcode_image(content: str | bytes, format_: str, scale: int = 4) -> Image.Image:
    """A barcode as a greyscale image, written by zxing-cpp."""
    code = zxingcpp.create_barcode(content, getattr(zxingcpp.BarcodeFormat, format_))
    raw = memoryview(code.to_image(scale=scale))
    height, width = raw.shape
    return Image.frombuffer("L", (width, height), bytes(raw))


def png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def make_pdf(
    texts: Sequence[tuple[float, float, str]] = (),
    images: Sequence[tuple[float, float, float, Image.Image]] = (),
    *,
    pages: int = 1,
) -> bytes:
    """
    A PDF with a text layer (Helvetica) and greyscale images.

    Args:
        texts: (x, y from the bottom, text) on every page, in points
        images: (x, y from the bottom, side length, image) on the first page
        pages: Number of pages; texts repeat on each, images are first-page only
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")
    page_tree = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    image_ids = []
    for _, _, _, image in images:
        grey = image.convert("L")
        data = zlib.compress(grey.tobytes())
        image_ids.append(add(
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
            b"/BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n%b\nendstream"
            % (grey.width, grey.height, len(data), data)
        ))
    page_ids = []
    for number in range(pages):
        ops = [b"BT /F1 11 Tf"]
        last_x = last_y = 0.0
        for x, y, text in texts:
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"{x - last_x:g} {y - last_y:g} Td ({escaped}) Tj".encode("latin-1"))
            last_x, last_y = x, y
        ops.append(b"ET")
        resources = b"/Font << /F1 %d 0 R >>" % font
        if number == 0 and images:
            for index, (x, y, side, _) in enumerate(images):
                ops.append(f"q {side:g} 0 0 {side:g} {x:g} {y:g} cm /Im{index} Do Q".encode())
            names = b" ".join(b"/Im%d %d 0 R" % (i, oid) for i, oid in enumerate(image_ids))
            resources += b" /XObject << " + names + b" >>"
        stream = b"\n".join(ops)
        content = add(b"<< /Length %d >>\nstream\n%b\nendstream" % (len(stream), stream))
        page_ids.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] /Contents %d 0 R "
            b"/Resources << %b >> >>" % (page_tree, content, resources)
        ))
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % page_tree
    kids = b" ".join(b"%d 0 R" % i for i in page_ids)
    objects[page_tree - 1] = b"<< /Type /Pages /Kids [%b] /Count %d >>" % (kids, len(page_ids))

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%b\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, catalog, xref,
    )
    return bytes(out)


def text_image(lines: Sequence[str], size: int = 34, width: int = 1500) -> Image.Image:
    """Black text on white, rendered with Pillow's bundled font."""
    font = ImageFont.load_default(size)
    image = Image.new("L", (width, 90 + len(lines) * (size + 26)), 255)
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(lines):
        draw.text((60, 50 + row * (size + 26)), line, fill=0, font=font)
    return image


def scanned_pdf(image: Image.Image) -> bytes:
    """A PDF whose only content is a picture, so it has no text layer."""
    out = io.BytesIO()
    image.convert("RGB").save(out, format="PDF", resolution=150.0)
    return out.getvalue()


def make_pkpass(barcodes: Sequence[dict[str, str]], **fields: object) -> bytes:
    """A minimal event-ticket pass."""
    pass_json = {
        "formatVersion": 1,
        "organizationName": "Example Cinema",
        "description": "Cinema ticket",
        "relevantDate": "2030-05-04T18:30:00+02:00",
        "eventTicket": {
            "primaryFields": [{"key": "movie", "label": "Film", "value": "Example Film"}],
            "secondaryFields": [{"key": "seat", "label": "Seat", "value": "Row 4, 7-8"}],
            "backFields": [{"key": "terms", "label": "Terms", "value": "No refunds"}],
        },
        "barcodes": list(barcodes),
        **fields,
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("pass.json", "﻿" + json.dumps(pass_json))
        archive.writestr("logo.png", b"not an image")
    return out.getvalue()
