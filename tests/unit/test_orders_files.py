"""
orders/files.py: how an upload is typed (by content and name, never by the
declared type) and the file names it stores. Every upload is accepted; one
that matches no known type is `other`.
"""

from __future__ import annotations

import pytest

from mail_verdict.orders.files import FILE_TYPES, clean_filename, detect_file_type


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
        ("shared", "Gate 12, boarding 10:40\nSeat 14C\n".encode(), "txt"),
        ("shared", "Abflug Düsseldorf".encode(), "txt"),
        ("anim.gif", b"GIF89a\x01\x00", "gif"),
        ("photo.webp", b"RIFF\x24\x00\x00\x00WEBPVP8 ", "webp"),
        ("scan.tif", b"II*\x00\x08\x00\x00\x00", "tiff"),
        ("scan.tiff", b"MM\x00*\x00\x00\x00\x08", "tiff"),
        ("photo.heic", b"\x00\x00\x00\x18ftypheic", "heic"),
        ("photo.heif", b"\x00\x00\x00\x18ftypmif1", "heic"),
        ("renamed.pdf", b"MZ\x90\x00 an executable", "other"),
        ("archive.zip", b"PK\x03\x04", "other"),
        ("letter.docx", b"PK\x03\x04\x14\x00\x06\x00", "other"),
        ("pass.pkpass", b"not a zip", "txt"),
        ("empty", b"", "other"),
        ("blob", b"abc\x00def", "other"),
        ("clip.wav", b"RIFF\x24\x00\x00\x00WAVEfmt ", "other"),
    ],
)
def test_a_file_is_judged_by_its_content_and_name(name: str, head: bytes, expected: str) -> None:
    assert detect_file_type(name, head) == expected


def test_every_type_has_a_content_type() -> None:
    assert FILE_TYPES["other"] == "application/octet-stream"
    assert all(FILE_TYPES.values())


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
