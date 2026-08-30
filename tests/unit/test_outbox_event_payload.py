"""The outbox.updated SSE payload must carry the row's current status.

PostIMAP's postimap_events NOTIFY only names which columns changed
(`changed: ["status"]`), never the new value, so forwarding it raw leaves
the frontend's `data.status` read always undefined -- the send/fail/dead
toasts and the Sent-folder invalidation never run.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault(
    "MAIL_VERDICT_DATABASE_URL", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"
)


def _make_db(row: object | None) -> MagicMock:
    """A fake DatabaseConnection whose session().execute() returns one row (or none)."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(one_or_none=MagicMock(return_value=row)))
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=False)
    return db


@pytest.mark.asyncio
async def test_outbox_payload_includes_status_and_kind_for_an_existing_row() -> None:
    from mail_verdict.server import _outbox_event_payload

    outbox_id = uuid.uuid4()
    row = SimpleNamespace(status="sent", kind="send")
    db = _make_db(row)
    event = SimpleNamespace(id=str(outbox_id), changed=("status",))

    payload = await _outbox_event_payload(db, event)

    assert payload["id"] == str(outbox_id)
    assert payload["changed"] == ["status"]
    assert payload["status"] == "sent"
    assert payload["kind"] == "send"


@pytest.mark.asyncio
async def test_outbox_payload_omits_status_when_the_row_is_gone() -> None:
    """No row (e.g. deleted between NOTIFY and read) degrades to the bare payload."""
    from mail_verdict.server import _outbox_event_payload

    db = _make_db(None)
    event = SimpleNamespace(id=str(uuid.uuid4()), changed=("status",))

    payload = await _outbox_event_payload(db, event)

    assert "status" not in payload
    assert "kind" not in payload
