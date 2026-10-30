"""
Shared setup for the pg tests of the orders pipeline: attachments and ticket
documents seeded for a mail, and a stand-in for the model calls that records
what the prompts held (the fixtures wiring them are in tests/pg/conftest.py).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers.documents import barcode_image, make_pdf


def ticket_pdf(
    number: str = "E551807712", code: str | bytes = "5471205382", format_: str = "QRCode",
    extra_lines: tuple[str, ...] = (),
) -> bytes:
    """A one-page ticket: an order number, a total, a route and a code under them."""
    lines = [
        f"Order number {number}", "Total EUR 19.98",
        "Route Heerlen to Amsterdam Centraal on 31 Oct 2026", *extra_lines,
    ]
    return make_pdf(
        texts=[(72, 780 - 16 * i, line) for i, line in enumerate(lines)],
        images=[(72, 560, 150, barcode_image(code, format_))],
    )


async def seed_attachment(
    session: AsyncSession, *, message_id: uuid.UUID, filename: str, content_type: str,
    data: bytes, content_id: str | None = None, attachment_id: uuid.UUID | None = None,
) -> uuid.UUID:
    attachment_id = attachment_id or uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO attachments "
            "(id, message_id, filename, content_type, content_id, size_bytes, data) "
            "VALUES (:id, :message_id, :filename, :content_type, :content_id, :size, :data)"
        ),
        {
            "id": attachment_id, "message_id": message_id, "filename": filename,
            "content_type": content_type, "content_id": content_id, "size": len(data),
            "data": data,
        },
    )
    return attachment_id


class ModelStub:
    """Stands in for `model_calls.structured_call`: records each call's
    prompts and schema, checks the answer against the call's own validator,
    and returns what the test queued for that task."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers: dict[str, Any] = {}

    async def __call__(
        self, db: object, cred_repo: object, settings_service: object, task: str, **kwargs: Any,
    ) -> tuple[dict[str, Any], str, float]:
        self.calls.append({"task": task, **kwargs})
        answer = self.answers[task]
        data = answer(kwargs) if callable(answer) else answer
        kwargs["validate"](data)
        return data, "stub", 1.0

    def last(self, task: str) -> dict[str, Any]:
        return [c for c in self.calls if c["task"] == task][-1]
