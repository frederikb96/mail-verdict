"""
Calendar files (.ics) as one short block of text per event.
"""

from __future__ import annotations

from datetime import date, datetime

import icalendar

from mail_verdict.orders.extract.model import Read

_MAX_EVENTS = 50
_DESCRIPTION_CHARS = 1_000


def _when(value: object) -> str:
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def read_ics(data: bytes) -> Read:
    """
    Summarise the events of a calendar file.

    Args:
        data: The .ics file

    Returns:
        Method `ics`; text with summary, start, end, location, organizer,
        status, link and a clipped description per event

    Raises:
        ValueError: The file is not a parseable calendar
    """
    calendar = icalendar.Calendar.from_ical(data.decode("utf-8", "replace"))
    blocks: list[str] = []
    method = calendar.get("method")
    for event in list(calendar.walk("VEVENT"))[:_MAX_EVENTS]:
        lines = [f"Event: {event.get('summary', '')}".rstrip()]
        if method:
            lines.append(f"Method: {method}")
        if "dtstart" in event:
            lines.append(f"Start: {_when(event.decoded('dtstart'))}")
        if "dtend" in event:
            lines.append(f"End: {_when(event.decoded('dtend'))}")
        for key, label in (
            ("location", "Location"), ("organizer", "Organizer"), ("status", "Status"),
            ("url", "Link"), ("uid", "Uid"),
        ):
            if event.get(key):
                lines.append(f"{label}: {str(event.get(key)).removeprefix('mailto:')}")
        description = str(event.get("description") or "").strip()
        if description:
            lines.append(f"Description: {description[:_DESCRIPTION_CHARS]}")
        blocks.append("\n".join(lines))
    return Read(method="ics", text="\n\n".join(blocks))
