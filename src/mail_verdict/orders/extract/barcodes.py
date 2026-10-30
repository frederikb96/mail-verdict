"""
Barcode reading and placement: zxing-cpp decodes QR, Aztec, Data Matrix,
PDF417 and the 1D formats from a rendered page or an image; each code is
placed into the document text at its reading-order position as a marker.

Why zxing-cpp: it is the one permissively licensed decoder that reads Aztec,
which Deutsche Bahn tickets use (a 290-byte binary UIC payload). Its `text`
rendering of such a payload is lossy, so the exact `bytes` are what is kept.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass, replace

import zxingcpp
from PIL import Image

from mail_verdict.orders.extract.model import Barcode

# Both 100 dpi and 150 dpi decode every QR and Aztec in a 70-PDF sample; 200 dpi
# only adds Code128 on pages where nothing was found, hence the second try.
SCAN_DPI = 150
RETRY_DPI = 200
RETRY_PAGES = 3
# A rendered page never exceeds this many pixels on a side (poster-sized PDFs).
MAX_RENDER_SIDE = 4_000
# Standalone images smaller than this on either side are logos and icons.
MIN_IMAGE_SIDE = 100

# Printable payloads up to this long are shown in the marker, longer or
# binary ones as a byte count.
_MARKER_TEXT_CHARS = 60
# Reading-order rows: tops within this many points count as one line.
ROW_POINTS = 6.0
_CROP_MARGIN = 0.12
_CROP_MAX_SIDE = 800

_FORMAT_NAMES = {
    "QRCode": "QR", "MicroQRCode": "MicroQR", "RMQRCode": "rMQR",
}


@dataclass(frozen=True)
class RawCode:
    """A decoded code before it has an index or a place in a text.
    `box` is (left, top, right, bottom) in the scanned image's pixels."""

    format: str
    payload: bytes
    text: str | None
    box: tuple[float, float, float, float]


def format_name(code: zxingcpp.Barcode) -> str:
    """The short, stable name stored and shown for a code's format."""
    name = code.format.name
    return _FORMAT_NAMES.get(name, name)


def scan(image: Image.Image) -> list[RawCode]:
    """
    Decode every barcode in an image.

    Args:
        image: Any PIL image (converted to greyscale for the decoder)

    Returns:
        The codes found, with pixel boxes; empty when there are none
    """
    found: list[RawCode] = []
    for code in zxingcpp.read_barcodes(image.convert("L")):
        pos = code.position
        xs = [pos.top_left.x, pos.top_right.x, pos.bottom_left.x, pos.bottom_right.x]
        ys = [pos.top_left.y, pos.top_right.y, pos.bottom_left.y, pos.bottom_right.y]
        payload = bytes(code.bytes)
        found.append(RawCode(
            format=format_name(code), payload=payload, text=printable_text(payload),
            box=(min(xs), min(ys), max(xs), max(ys)),
        ))
    return found


def printable_text(payload: bytes) -> str | None:
    """The payload as text when it is valid UTF-8 without control characters."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if text and text.isprintable() else None


def marker(index: int, format_: str, payload: bytes, text: str | None) -> str:
    """The placeholder that stands for a code inside document text."""
    if text is not None and len(text) <= _MARKER_TEXT_CHARS and '"' not in text and "]" not in text:
        return f'[code b{index} {format_} "{text}"]'
    return f"[code b{index} {format_} {len(payload)} bytes]"


def crop_png(image: Image.Image, box: tuple[float, float, float, float]) -> bytes:
    """A PNG of the code with a quiet-zone margin, kept as a fallback rendering
    for formats the writer cannot re-encode."""
    left, top, right, bottom = box
    margin = max(right - left, bottom - top) * _CROP_MARGIN
    region = image.convert("L").crop((
        max(0, int(left - margin)), max(0, int(top - margin)),
        min(image.width, int(right + margin) + 1), min(image.height, int(bottom + margin) + 1),
    ))
    region.thumbnail((_CROP_MAX_SIDE, _CROP_MAX_SIDE))
    out = io.BytesIO()
    region.save(out, format="PNG", optimize=True)
    return out.getvalue()


def render_code(payload: bytes, format_: str, text: str | None, scale: int) -> bytes | None:
    """
    Re-encode a stored payload as a PNG for display.

    Args:
        payload: The code's exact bytes
        format_: The stored format name
        text: The printable text, if the payload had one
        scale: Pixels per module

    Returns:
        PNG bytes, or None when the format has no writer
    """
    names = {v: k for k, v in _FORMAT_NAMES.items()}
    fmt = getattr(zxingcpp.BarcodeFormat, names.get(format_, format_), None)
    if fmt is None:
        return None
    try:
        code = zxingcpp.create_barcode(text if text is not None else payload, fmt)
        raw = memoryview(code.to_image(scale=scale))
    except (ValueError, TypeError, RuntimeError):
        return None
    if raw.shape is None or len(raw.shape) != 2:
        return None
    height, width = raw.shape
    out = io.BytesIO()
    Image.frombuffer("L", (width, height), bytes(raw)).save(out, format="PNG")
    return out.getvalue()


def insert_markers(
    text: str,
    anchors: Sequence[tuple[int, float, float]],
    placed: Sequence[tuple[str, float, float]],
) -> str:
    """
    Put code markers into a text at their reading-order position.

    A marker goes before the line of the first anchor that follows it in
    reading order (rows of `ROW_POINTS`, then left to right); one after every
    anchor goes at the end.

    Args:
        text: Page text
        anchors: (character index, top, left) of glyphs in the text, same
            unit as `placed`
        placed: (marker, top, left) of each code
    """
    inserts: list[tuple[int, int, str]] = []
    for order, (marker_text, top, left) in enumerate(placed):
        key = (round(top / ROW_POINTS), left)
        later = [a for a in anchors if (round(a[1] / ROW_POINTS), a[2]) > key]
        if later:
            index = min(later, key=lambda a: (round(a[1] / ROW_POINTS), a[2]))[0]
            at = text.rfind("\n", 0, index) + 1
        else:
            at = len(text)
        inserts.append((at, order, marker_text))
    # Back to front, so earlier insert positions stay valid.
    for at, _, marker_text in sorted(inserts, key=lambda i: (i[0], i[1]), reverse=True):
        tail = text[at:]
        if at < len(text):
            text = f"{text[:at]}{marker_text}\n{tail}"
        else:
            text = f"{text}{chr(10) if text and not text.endswith(chr(10)) else ''}{marker_text}"
    return text


class CodeBook:
    """The codes of one document, numbered 1..n in discovery order. A repeat
    of the same format and payload is dropped (a ticket printing its code
    twice is one fact)."""

    def __init__(self) -> None:
        self.codes: list[Barcode] = []

    def add(
        self,
        code: RawCode,
        *,
        page: int | None,
        bbox: tuple[float, float, float, float] | None,
        crop: bytes | None,
    ) -> Barcode | None:
        """Number a code; None when it repeats an earlier one."""
        if any(c.format == code.format and c.payload == code.payload for c in self.codes):
            return None
        barcode = Barcode(
            index=len(self.codes) + 1, format=code.format, payload=code.payload,
            text=code.text, page=page, bbox=bbox, text_offset=None, crop_png=crop,
        )
        self.codes.append(barcode)
        return barcode

    def with_offsets(self, text: str) -> tuple[Barcode, ...]:
        """The codes with `text_offset` set to where their marker sits in `text`."""
        out = []
        for code in self.codes:
            at = text.find(f"[code b{code.index} ")
            out.append(replace(code, text_offset=at if at >= 0 else None))
        return tuple(out)
