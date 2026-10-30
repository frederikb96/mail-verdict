"""
orders/files.py: which uploads the register accepts (by content and name,
never by the declared type) and the file names it stores.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mail_verdict.orders.files import FILE_TYPES, clean_filename, detect_file_type

_CONFIG = Path(__file__).parent.parent.parent / "config" / "config.yaml"


@pytest.mark.parametrize(
    ("name", "head", "expected"),
    [
        ("ticket.pdf", b"%PDF-1.7 ...", "pdf"),
        ("scan", b"%PDF-1.4", "pdf"),
        ("photo.png", b"\x89PNG\r\n\x1a\n", "png"),
        ("photo.jpg", b"\xff\xd8\xff\xe0", "jpeg"),
        ("pass.pkpass", b"PK\x03\x04", "pkpass"),
        ("trip.ics", b"BEGIN:VCALENDAR\n", "ics"),
        ("trip", b"  BEGIN:VCALENDAR\nVERSION:2.0", "ics"),
        ("invoice.xml", b"<?xml version='1.0'?>", "xml"),
        ("page.html", b"<!DOCTYPE html><html>", "html"),
        ("note.txt", b"hello", "txt"),
        ("renamed.pdf", b"MZ\x90\x00 an executable", None),
        ("archive.zip", b"PK\x03\x04", None),
        ("photo.heic", b"\x00\x00\x00\x18ftypheic", None),
        ("pass.pkpass", b"not a zip", None),
    ],
)
def test_a_file_is_judged_by_its_content_and_name(name: str, head: bytes, expected: str) -> None:
    assert detect_file_type(name, head) == expected


def test_every_type_the_config_accepts_has_a_content_type() -> None:
    with open(_CONFIG) as handle:
        accepted = yaml.safe_load(handle)["order_files"]["accepted_types"]
    assert set(accepted) <= set(FILE_TYPES)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ticket.pdf", "ticket.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\scan.pdf", "scan.pdf"),
        ("bad\x00name\n.pdf", "badname.pdf"),
        ("   ", "file"),
        (None, "file"),
        ("x" * 500 + ".pdf", ("x" * 500 + ".pdf")[:200]),
    ],
)
def test_a_file_name_is_a_bare_bounded_name(raw: str | None, expected: str) -> None:
    assert clean_filename(raw) == expected
