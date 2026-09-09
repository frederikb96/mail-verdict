"""
A bounded, process-local cache in front of ical.py's expand_instances() --
the same VEVENT body gets re-parsed and re-walked on every month request
that touches it, regardless of whether anything about the object changed
between requests, and 85% of a month view's own CPU cost is exactly this
repeated work over the same handful of recurring series.

Keyed on (dav_objects.id, etag[, window]) rather than invalidated on
write: a changed object simply gets a different key, so a missed
invalidation costs memory, never a stale answer. A pending write (etag
IS NULL -- an insert or move not yet confirmed by the server) has no
stable identity to key on and is never cached; etag settles within
milliseconds of the write that produced it.

Two levels, matching what actually costs time:
  - the parsed Calendar plus recurring-ical-events' own query object
    (ical.ExpansionQuery) -- icalendar parsing, paid once per object
    however many windows it is later asked about;
  - the expanded occurrence list for one exact window -- a revisited
    month becomes a dict lookup.

Thread-safe: _expand_all_sync (calendar_events.py) runs on a worker
thread, and several requests' worker threads can reach this
concurrently.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import OrderedDict
from datetime import datetime
from typing import TypeVar

from mail_verdict.calendar import ical

logger = logging.getLogger(__name__)

# The whole seeded corpus this was measured against (~700 KB of iCalendar
# text across ~1250 objects) fits in a few tens of megabytes parsed --
# these ceilings are generous against that, not a promise about a much
# larger real account, which is the thing to measure before raising them.
_MAX_QUERY_ENTRIES = 4096
_MAX_EXPANSION_ENTRIES = 16384

_K = TypeVar("_K")
_V = TypeVar("_V")


class _LruCache(dict[_K, _V]):
    """The stdlib has no bounded ordered dict of its own -- OrderedDict
    plus a max-size eviction on insert is the whole of what LRU means
    here, so this stays over subclassing something heavier."""

    def __init__(self, max_entries: int) -> None:
        super().__init__()
        self._order: OrderedDict[_K, None] = OrderedDict()
        self._max_entries = max_entries

    def get_hit(self, key: _K) -> _V | None:
        if key not in self:
            return None
        self._order.move_to_end(key)
        return self[key]

    def put(self, key: _K, value: _V) -> None:
        self[key] = value
        self._order[key] = None
        self._order.move_to_end(key)
        while len(self) > self._max_entries:
            oldest, _ = self._order.popitem(last=False)
            del self[oldest]


_query_cache: _LruCache[tuple[uuid.UUID, str], ical.ExpansionQuery] = _LruCache(_MAX_QUERY_ENTRIES)
_expansion_cache: _LruCache[
    tuple[uuid.UUID, str, datetime, datetime], list[ical.ParsedEvent]
] = _LruCache(_MAX_EXPANSION_ENTRIES)
_lock = threading.Lock()

_hits = 0
_misses = 0


def occurrences_for(
    object_id: uuid.UUID, etag: str | None, data: str,
    window_start: datetime, window_end: datetime,
) -> list[ical.ParsedEvent]:
    """expand_instances(), cached by (object_id, etag[, window]). Falls
    back to the uncached call for a pending object, which has no etag to
    key on yet."""
    global _hits, _misses
    if etag is None:
        return ical.expand_instances(data, window_start, window_end)

    expansion_key = (object_id, etag, window_start, window_end)
    with _lock:
        cached = _expansion_cache.get_hit(expansion_key)
        if cached is not None:
            _hits += 1
        else:
            _misses += 1
        hits, misses = _hits, _misses
    _log_rate(hits, misses)
    if cached is not None:
        return cached

    query_key = (object_id, etag)
    with _lock:
        query = _query_cache.get_hit(query_key)
    if query is None:
        query = ical.build_expansion_query(data)
        with _lock:
            _query_cache.put(query_key, query)

    result = ical.expand_from_query(query, window_start, window_end)
    with _lock:
        _expansion_cache.put(expansion_key, result)
    return result


def _log_rate(hits: int, misses: int) -> None:
    total = hits + misses
    # Every 500th call rather than every call -- frequent enough to
    # watch the rate settle in as a real account's month views revisit
    # the same objects, cheap enough not to be its own cost.
    if total % 500 == 0:
        logger.info(
            "Calendar expansion cache hit rate",
            extra={"hits": hits, "misses": misses, "hit_rate": hits / total if total else 0.0},
        )
