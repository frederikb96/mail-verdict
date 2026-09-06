"""The one place that decides whether a calendar is offered at all.

`calendar_prefs.is_enabled` is nullable on purpose: NULL means nobody has
decided, not "off". Every surface that asks the question -- the calendar
list the sidebar and the manage dialog render, and the month view's own
filter -- reads the answer from here, so a collection cannot be offered
in one place and absent from another.
"""

from __future__ import annotations

from mail_verdict.database.models import CalendarPrefs, DavCollection


def calendar_is_enabled(collection: DavCollection, prefs: CalendarPrefs | None) -> bool:
    """Whether this calendar is offered: the stored choice when somebody
    made one, otherwise whether the collection can hold an event at all.

    A to-do-only collection (a Nextcloud task list, typically) has nothing
    a month view can ever draw, so it stays out until somebody opts it in
    through the manage dialog -- and it stays out however many prefs rows
    already exist for it, since a row written for some unrelated reason
    leaves this column NULL.
    """
    if prefs is not None and prefs.is_enabled is not None:
        return prefs.is_enabled
    return collection.supports_vevent
