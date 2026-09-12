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


def _make_db(row: object | None, *, reply_id: uuid.UUID | None = None) -> MagicMock:
    """A fake DatabaseConnection whose session().execute() returns one row (or none),
    and whose session().scalar() reports whether the outbox row is linked from
    calendar_replies (reply_id) or not (None)."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(one_or_none=MagicMock(return_value=row)))
    session.scalar = AsyncMock(return_value=reply_id)
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


@pytest.mark.asyncio
async def test_outbox_payload_marks_an_rsvp_reply_row_as_itip_reply() -> None:
    """A send row calendar_replies.outbox_id points at gets itip="reply", so
    the frontend refreshes the invitation card and the calendar event instead
    of showing a mail-send toast for a message nobody composed."""
    from mail_verdict.server import _outbox_event_payload

    outbox_id = uuid.uuid4()
    row = SimpleNamespace(status="sent", kind="send")
    db = _make_db(row, reply_id=uuid.uuid4())
    event = SimpleNamespace(id=str(outbox_id), changed=("status",))

    payload = await _outbox_event_payload(db, event)

    assert payload["itip"] == "reply"


@pytest.mark.asyncio
async def test_outbox_payload_omits_itip_for_an_ordinary_send() -> None:
    """A send no calendar_replies row points at (an ordinary mail, or an
    itip REQUEST/CANCEL the organizer side sends) carries no itip field."""
    from mail_verdict.server import _outbox_event_payload

    outbox_id = uuid.uuid4()
    row = SimpleNamespace(status="sent", kind="send")
    db = _make_db(row, reply_id=None)
    event = SimpleNamespace(id=str(outbox_id), changed=("status",))

    payload = await _outbox_event_payload(db, event)

    assert "itip" not in payload
