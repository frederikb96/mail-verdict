"""
Apple Wallet passes (.pkpass): a zip whose pass.json carries the pass's
fields and its barcode message, so nothing needs decoding from an image.
"""

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from typing import Any

from mail_verdict.orders.extract import barcodes as bc
from mail_verdict.orders.extract.model import Read

# pass.json is a few KB; anything near this size is not a pass.
_MAX_PASS_JSON_BYTES = 1_000_000
_FIELD_GROUPS = (
    "headerFields", "primaryFields", "secondaryFields", "auxiliaryFields", "backFields",
)
_STYLES = ("eventTicket", "boardingPass", "coupon", "storeCard", "generic")
_FORMATS = {
    "PKBarcodeFormatQR": "QR",
    "PKBarcodeFormatAztec": "Aztec",
    "PKBarcodeFormatPDF417": "PDF417",
    "PKBarcodeFormatCode128": "Code128",
}


def _field_line(item: dict[str, Any]) -> str | None:
    value = item.get("value")
    if value is None or value == "":
        return None
    label = item.get("label") or item.get("key") or ""
    return f"{label}: {value}" if label else str(value)


def read_pkpass(data: bytes) -> Read:
    """
    Read a pass into text and its barcodes.

    Args:
        data: The .pkpass file

    Returns:
        Method `pkpass`; the text lists the pass's fields, then its codes

    Raises:
        zipfile.BadZipFile, KeyError, ValueError: Not a readable pass
    """
    with zipfile.ZipFile(BytesIO(data)) as archive:
        with archive.open("pass.json") as handle:
            raw = handle.read(_MAX_PASS_JSON_BYTES + 1)
    if len(raw) > _MAX_PASS_JSON_BYTES:
        raise ValueError("pass.json is too large")
    # Passes in the wild carry a BOM.
    pass_json: dict[str, Any] = json.loads(raw.decode("utf-8-sig"))

    lines: list[str] = []
    title = " - ".join(
        str(pass_json[k]) for k in ("description", "organizationName") if pass_json.get(k)
    )
    if title:
        lines.append(f"Pass: {title}")
    for key, label in (("relevantDate", "Relevant date"), ("expirationDate", "Expires")):
        if pass_json.get(key):
            lines.append(f"{label}: {pass_json[key]}")

    style = next((s for s in _STYLES if isinstance(pass_json.get(s), dict)), None)
    groups: dict[str, Any] = pass_json[style] if style else {}
    for group in _FIELD_GROUPS:
        for item in groups.get(group) or []:
            if isinstance(item, dict) and (line := _field_line(item)):
                lines.append(line)

    declared = list(pass_json.get("barcodes") or [])
    if isinstance(pass_json.get("barcode"), dict):
        declared.append(pass_json["barcode"])
    book = bc.CodeBook()
    for entry in declared:
        message = entry.get("message")
        name = _FORMATS.get(str(entry.get("format")))
        if not isinstance(message, str) or name is None:
            continue
        encoding = str(entry.get("messageEncoding") or "utf-8")
        try:
            payload = message.encode(encoding)
        except (LookupError, UnicodeEncodeError):
            payload = message.encode("utf-8")
        code = book.add(
            bc.RawCode(name, payload, bc.printable_text(payload), (0, 0, 0, 0)),
            page=None, bbox=None, crop=None,
        )
        if code is not None:
            lines.append(bc.marker(code.index, code.format, code.payload, code.text))
    text = "\n".join(lines)
    return Read(method="pkpass", text=text, barcodes=book.with_offsets(text))
