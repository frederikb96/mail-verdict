"""The one place that decides whether a calendar is offered at all.

`calendar_prefs.is_enabled` is nullable on purpose: NULL means nobody has
decided, not "off". Every surface that asks the question -- the calendar
list the sidebar and the manage dialog render, and the month view's own
filter -- reads the answer from here, so a collection cannot be offered
in one place and absent from another.
"""

from __future__ import annotations

from typing import Any

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


def resolve_default_reminder(
    prefs: CalendarPrefs | None, settings: dict[str, Any],
) -> int | None:
    """How many minutes before an event's start a freshly created event on
    this calendar should default to reminding at -- or None for no default
    reminder at all. Two nullable questions resolved together: whether a
    default exists at all (reminders_enabled) and how long before it falls
    (default_reminder_minutes). Either calendar_prefs column left NULL
    means inherit; reminders_enabled=False switches this calendar's
    default off regardless of what the global setting says -- there is no
    integer sentinel for "off" here, since 0 is a legitimate at-start-time
    reminder.

    Callers apply this only when the editor's create form opens, never
    implicitly on the server at save time: a server-side injection would
    put alarms on events the MCP tools and invitation intake create, where
    nobody asked for one and nothing in the form shows it.
    """
    if prefs is not None and prefs.reminders_enabled is False:
        return None
    if prefs is not None and prefs.default_reminder_minutes is not None:
        return prefs.default_reminder_minutes
    global_default = settings.get("default_reminder_minutes")
    return global_default if isinstance(global_default, int) else None
