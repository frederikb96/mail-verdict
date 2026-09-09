"""The calendar.object SSE payload must carry enough for a listener to
invalidate only the months a change actually touches, rather than every
month it has mounted -- see _dav_object_event_context's own docstring.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
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
async def test_context_includes_dtstart_dtend_and_is_recurring_for_a_calendar_object() -> None:
    from mail_verdict.server import _dav_object_event_context

    dtstart = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)
    dtend = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)
    row = SimpleNamespace(kind="calendar", dtstart=dtstart, dtend=dtend, is_recurring=True)
    db = _make_db(row)
    event = SimpleNamespace(id=str(uuid.uuid4()), collection_id=str(uuid.uuid4()))

    context = await _dav_object_event_context(db, event)

    assert context.kind == "calendar"
    assert context.dtstart == dtstart
    assert context.dtend == dtend
    assert context.is_recurring is True


@pytest.mark.asyncio
async def test_context_degrades_to_empty_when_the_collection_id_is_absent() -> None:
    """A dav_object event with no collection_id at all never reaches the database."""
    from mail_verdict.server import _dav_object_event_context

    db = _make_db(SimpleNamespace(kind="calendar", dtstart=None, dtend=None, is_recurring=False))
    event = SimpleNamespace(id=str(uuid.uuid4()), collection_id=None)

    context = await _dav_object_event_context(db, event)

    assert context == (None, None, None, None)
    db.session.assert_not_called()


@pytest.mark.asyncio
async def test_context_degrades_to_empty_when_the_collection_id_does_not_parse() -> None:
    from mail_verdict.server import _dav_object_event_context

    db = _make_db(SimpleNamespace(kind="calendar", dtstart=None, dtend=None, is_recurring=False))
    event = SimpleNamespace(id=str(uuid.uuid4()), collection_id="not-a-uuid")

    context = await _dav_object_event_context(db, event)

    assert context == (None, None, None, None)
    db.session.assert_not_called()


@pytest.mark.asyncio
async def test_context_is_empty_when_the_collection_row_is_already_gone() -> None:
    """A delete event's own collection can outlive the row it named -- see
    _dav_collection_kind's docstring, which this mirrors."""
    from mail_verdict.server import _dav_object_event_context

    db = _make_db(None)
    event = SimpleNamespace(id=str(uuid.uuid4()), collection_id=str(uuid.uuid4()))

    context = await _dav_object_event_context(db, event)

    assert context == (None, None, None, None)


@pytest.mark.asyncio
async def test_object_fields_are_null_when_the_object_row_is_gone_but_the_collection_is_not() -> (
    None
):
    """The outer join keeps the collection's kind even when the object
    itself has been deleted and purged -- a calendar.object for a purge
    still needs to know it isn't a contact.object."""
    from mail_verdict.server import _dav_object_event_context

    row = SimpleNamespace(kind="calendar", dtstart=None, dtend=None, is_recurring=None)
    db = _make_db(row)
    event = SimpleNamespace(id=str(uuid.uuid4()), collection_id=str(uuid.uuid4()))

    context = await _dav_object_event_context(db, event)

    assert context.kind == "calendar"
    assert context.dtstart is None
    assert context.dtend is None
    assert context.is_recurring is None
