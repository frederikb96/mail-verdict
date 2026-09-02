"""
Calendars and contacts on top of the DAV contract.

MailVerdict never speaks CalDAV or CardDAV -- PostIMAP does, mirroring
each resource into dav_objects.data as the verbatim iCalendar or vCard
body (see postimap/contract.py and the consumer contract's "Calendars and
contacts" section). This package is where that body is read and built:

- `ical.py` -- parsing and constructing VEVENT/VCALENDAR bodies with the
  `icalendar` library, and `recurring-ical-events` for date-range
  expansion. Never writes dav_objects SQL directly.
- `vcard.py` -- parsing and constructing VCARD bodies with `vobject`.
- `repository.py` -- the owned tables (calendar_prefs, calendar_intake,
  calendar_replies), the only place their SQL lives.
"""

from __future__ import annotations
