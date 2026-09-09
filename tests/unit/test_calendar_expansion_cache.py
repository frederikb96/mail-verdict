"""
calendar/expansion_cache.py -- keyed on (object id, etag[, window]) rather
than invalidated on write, so correctness never depends on an eviction
signal arriving.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from mail_verdict.calendar import expansion_cache, ical

_RECURRING = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\nBEGIN:VEVENT\r\n"
    "UID:cache-1@example.com\r\nDTSTAMP:20260101T000000Z\r\n"
    "DTSTART:20200101T090000Z\r\nDTEND:20200101T093000Z\r\n"
    "SUMMARY:Weekly\r\nRRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)
_WINDOW_START = datetime(2026, 9, 1, tzinfo=timezone.utc)
_WINDOW_END = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_a_second_call_with_the_same_etag_never_reparses() -> None:
    object_id = uuid.uuid4()
    first = expansion_cache.occurrences_for(
        object_id, "etag-1", _RECURRING, _WINDOW_START, _WINDOW_END,
    )
    assert len(first) > 0

    with patch.object(
        ical, "build_expansion_query", wraps=ical.build_expansion_query,
    ) as build_spy:
        second = expansion_cache.occurrences_for(
            object_id, "etag-1", _RECURRING, _WINDOW_START, _WINDOW_END,
        )
    build_spy.assert_not_called()
    assert [i.dtstart for i in second] == [i.dtstart for i in first]


def test_a_different_etag_is_a_different_key_not_a_stale_hit() -> None:
    """Correctness never depends on invalidation arriving -- a changed
    object simply gets a different key, so revisiting it after a body
    change (a new etag) still re-parses rather than returning the old
    body's occurrences."""
    object_id = uuid.uuid4()
    expansion_cache.occurrences_for(object_id, "etag-1", _RECURRING, _WINDOW_START, _WINDOW_END)

    changed = _RECURRING.replace("SUMMARY:Weekly", "SUMMARY:Weekly (renamed)")
    with patch.object(
        ical, "build_expansion_query", wraps=ical.build_expansion_query,
    ) as build_spy:
        result = expansion_cache.occurrences_for(
            object_id, "etag-2", changed, _WINDOW_START, _WINDOW_END,
        )
    build_spy.assert_called_once()
    assert result[0].summary == "Weekly (renamed)"


def test_a_pending_object_with_no_etag_is_never_cached() -> None:
    """etag IS NULL means the write has not been confirmed yet -- no
    stable identity to key on, and caching a pending body would risk
    serving it back after the confirmed body (and its real etag) lands."""
    object_id = uuid.uuid4()
    with patch.object(
        ical, "build_expansion_query", wraps=ical.build_expansion_query,
    ) as build_spy:
        expansion_cache.occurrences_for(object_id, None, _RECURRING, _WINDOW_START, _WINDOW_END)
        expansion_cache.occurrences_for(object_id, None, _RECURRING, _WINDOW_START, _WINDOW_END)
    assert build_spy.call_count == 2


def test_revisiting_the_same_object_in_a_different_window_reuses_the_parse() -> None:
    """The level-1 cache (the parsed query, not the expanded result) is
    what a scrolled-to month actually hits -- a new window still needs
    its own between() walk, but never re-parses the body for it."""
    object_id = uuid.uuid4()
    expansion_cache.occurrences_for(object_id, "etag-1", _RECURRING, _WINDOW_START, _WINDOW_END)

    other_start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    other_end = datetime(2026, 11, 1, tzinfo=timezone.utc)
    with patch.object(
        ical, "build_expansion_query", wraps=ical.build_expansion_query,
    ) as build_spy, patch.object(
        ical, "expand_from_query", wraps=ical.expand_from_query,
    ) as expand_spy:
        result = expansion_cache.occurrences_for(
            object_id, "etag-1", _RECURRING, other_start, other_end,
        )
    build_spy.assert_not_called()
    expand_spy.assert_called_once()
    assert len(result) > 0
