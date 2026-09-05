"""
Keyset (cursor) pagination over a (timestamp, id) ordering, where the
timestamp column is nullable.

A message list is ordered `desc(received_at), desc(id)`. PostgreSQL sorts
NULL as the greatest value in a DESC ordering by default (NULLS FIRST), so
a message with no received_at sorts ahead of every dated one. Continuing
past such a row needs to account for that placement explicitly -- a plain
`received_at < cursor_received_at` is never true when either side is NULL
(SQL's three-valued logic treats the comparison as unknown, which a WHERE
clause discards as if it were false), so a naive predicate either resets
to the first page (the filter silently applying to nothing) or drops the
rest of the list (excluding every remaining row). Both have shown up here:
a cursor whose row had a NULL received_at either repeated the page it was
supposed to advance past, or ended pagination early depending on which of
the two ways the predicate was written.

Search orders `desc(received_at) NULLS LAST` instead -- a message with no
date header (no header to sort by, at all) belongs at the bottom of a
newest-first list, not pinned above every dated result -- so `after_cursor`
takes which placement to match rather than assuming Postgres's own default.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, and_, or_
from sqlalchemy.orm import InstrumentedAttribute


def after_cursor(
    received_at_col: InstrumentedAttribute[datetime | None],
    id_col: InstrumentedAttribute[uuid.UUID],
    cursor_received_at: datetime | None,
    cursor_id: uuid.UUID,
    *,
    nulls_last: bool = False,
) -> ColumnElement[bool]:
    """
    Build the WHERE predicate for "every row after this cursor", matching
    an `ORDER BY received_at DESC, id DESC` with PostgreSQL's default
    NULLS FIRST placement for DESC -- or, with `nulls_last=True`, an
    explicit `ORDER BY received_at DESC NULLS LAST, id DESC` instead.

    Args:
        received_at_col: The nullable timestamp column driving the order
        id_col: The tiebreaker column, unique and never null
        cursor_received_at: received_at of the last row of the previous page
        cursor_id: id of the last row of the previous page
        nulls_last: Match an explicit NULLS LAST ordering instead of
            Postgres's own NULLS FIRST default for DESC

    Returns:
        A predicate correct whether or not the cursor row had a NULL
        received_at, and whether or not any later row does either.
    """
    if cursor_received_at is None:
        if nulls_last:
            # The NULL group sorts entirely last, and the cursor row was
            # in it -- everything "after" it is the rest of that group.
            return and_(received_at_col.is_(None), id_col < cursor_id)
        # The cursor row was itself in the NULL group, which sorts first.
        # The rest of that group (smaller id) comes next, then every
        # non-NULL row -- all of which sort after the whole NULL group.
        return or_(
            and_(received_at_col.is_(None), id_col < cursor_id),
            received_at_col.is_not(None),
        )
    if nulls_last:
        # The NULL group sorts entirely last, so it counts as "after" any
        # dated cursor row, on top of the ordinary date/id comparison.
        return or_(
            received_at_col < cursor_received_at,
            and_(received_at_col == cursor_received_at, id_col < cursor_id),
            received_at_col.is_(None),
        )
    return or_(
        received_at_col < cursor_received_at,
        and_(received_at_col == cursor_received_at, id_col < cursor_id),
    )
