"""
Tests for the outbox attachment-read cap (_read_capped_attachment).

config/config.yaml documents outbox.max_attachment_bytes as enforced while
the upload is being read, because the cost being bounded is memory -- these
tests prove the read actually stops near the limit rather than buffering
the whole upload first and measuring it afterwards.
"""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from mail_verdict.api.outbox import _ATTACHMENT_READ_CHUNK_BYTES, _read_capped_attachment


class _FakeUpload:
    """
    A duck-typed stand-in for UploadFile that hands out bytes in chunks and
    counts how many were actually pulled through .read() -- the read count
    is the thing that proves early abort, not just the final exception.
    """

    def __init__(self, total_bytes: int, filename: str = "attachment.bin") -> None:
        self.filename = filename
        self._remaining = total_bytes
        self.bytes_read = 0

    async def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        take = min(size, self._remaining) if size and size > 0 else self._remaining
        self._remaining -= take
        self.bytes_read += take
        return b"x" * take


@pytest.mark.asyncio
async def test_oversized_upload_is_rejected_without_reading_it_whole() -> None:
    """
    An upload far larger than the limit must stop within roughly one read
    chunk of the limit, not after the entire stream has passed through --
    a plain `await value.read()` followed by a length check would show
    bytes_read equal to the full stream instead.
    """
    max_bytes = 5 * 1024 * 1024
    total_bytes = 50 * 1024 * 1024
    upload = _FakeUpload(total_bytes=total_bytes)

    with pytest.raises(HTTPException) as exc_info:
        await _read_capped_attachment(cast(UploadFile, upload), max_bytes)

    assert exc_info.value.status_code == 413
    assert upload.bytes_read <= max_bytes + _ATTACHMENT_READ_CHUNK_BYTES
    assert upload.bytes_read < total_bytes


@pytest.mark.asyncio
async def test_upload_within_the_limit_is_returned_whole() -> None:
    """An upload under the cap is read in full and returned unchanged."""
    upload = _FakeUpload(total_bytes=100)

    content = await _read_capped_attachment(cast(UploadFile, upload), max_bytes=1000)

    assert len(content) == 100
    assert upload.bytes_read == 100


@pytest.mark.asyncio
async def test_upload_exactly_at_the_limit_is_accepted() -> None:
    """The boundary itself is not rejected -- only strictly over it is."""
    upload = _FakeUpload(total_bytes=1000)

    content = await _read_capped_attachment(cast(UploadFile, upload), max_bytes=1000)

    assert len(content) == 1000
