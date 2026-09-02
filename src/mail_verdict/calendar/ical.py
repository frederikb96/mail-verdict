"""
Reading and building iCalendar (VEVENT) bodies.

The unit of sync is the whole resource -- dav_objects.data holds one
VCALENDAR per UID, the master VEVENT plus its RECURRENCE-ID exceptions
exactly as the server stores them (consumer-contract.md's "Calendars and
contacts"). Recurrence is never expanded in storage; expand_instances()
is the read-time consumer concern the contract documents.

Every function here is pure: given a body (or a body and some fields),
return a new body or a parsed view. Nothing here touches the database --
calendar/repository.py and the API routers are what call these and hand
the result to postimap/actions.py.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, cast

import recurring_ical_events
from icalendar import Calendar, Component, Event, vCalAddress, vText


def _serialize(component: Any) -> str:
    """icalendar's to_ical()/decode() chain is untyped -- this is the one
    place that tells mypy the result is the str every function here
    promises to return."""
    return cast(str, component.to_ical().decode("utf-8"))


# icalendar represents a single-valued property (ORGANIZER, one ATTENDEE)
# as the bare value, and a multi-valued one (several ATTENDEEs) as a list
# -- this is what normalises both shapes to a list, always. Typed Any
# rather than the vCalAddress icalendar actually returns: every caller
# only ever reads .params off the result, which a stricter type would not
# make any safer here.
def _as_list(value: Any | None) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _addr_email(addr: object) -> str:
    """The bare email from a vCalAddress/str value, stripping mailto:."""
    text = str(addr)
    if text.lower().startswith("mailto:"):
        text = text[7:]
    return text.strip()


def _addr_cn(addr: Any) -> str | None:
    params = getattr(addr, "params", None)
    if not params:
        return None
    cn = params.get("CN")
    return str(cn) if cn else None


@dataclass
class Organizer:
    email: str
    cn: str | None = None


@dataclass
class Attendee:
    email: str
    cn: str | None = None
    partstat: str = "needs-action"
    role: str = "req-participant"


@dataclass
class ParsedEvent:
    """One VEVENT component -- the master, an exception, or an expanded
    instance. dtstart/dtend/all_day/tz describe this component's own
    occurrence; recurrence_id is None on the master."""

    uid: str
    summary: str
    dtstart: datetime
    dtend: datetime
    tz: str | None
    all_day: bool
    location: str | None
    description: str | None
    status: str
    sequence: int
    organizer: Organizer | None
    attendees: list[Attendee] = field(default_factory=list)
    recurrence_id: str | None = None
    rrule: str | None = None
    is_recurring: bool = False
    is_exception: bool = False


@dataclass
class ParsedInvitation:
    """A whole VCALENDAR lifted out of an email attachment -- METHOD plus
    the master component (and any exception components alongside it)."""

    method: str
    master: ParsedEvent
    exceptions: list[ParsedEvent]


def _partstat_of(addr: Any) -> str:
    params = getattr(addr, "params", None)
    raw = str(params.get("PARTSTAT", "NEEDS-ACTION")) if params else "NEEDS-ACTION"
    return raw.lower().replace("_", "-")


def _role_of(addr: Any) -> str:
    params = getattr(addr, "params", None)
    raw = str(params.get("ROLE", "REQ-PARTICIPANT")) if params else "REQ-PARTICIPANT"
    return raw.lower()


def _to_datetime(value: object, all_day: bool) -> datetime:
    """icalendar hands back a date for an all-day VEVENT, a datetime
    otherwise -- normalise to a timezone-aware datetime either way, since
    every consumer of ParsedEvent expects one."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    raise ValueError(f"Unsupported DTSTART/DTEND value: {value!r}")


def _tz_name(value: object) -> str | None:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return getattr(value.tzinfo, "key", None) or str(value.tzinfo)
    return None


def _parse_component(
    component: Component, *, is_recurring: bool, is_exception: bool,
) -> ParsedEvent:
    dtstart_prop = component.get("DTSTART")
    if dtstart_prop is None:
        raise ValueError("VEVENT has no DTSTART")
    dtstart_val = dtstart_prop.dt
    all_day = not isinstance(dtstart_val, datetime)

    dtend_prop = component.get("DTEND")
    if dtend_prop is not None:
        dtend_val = dtend_prop.dt
    else:
        duration = component.get("DURATION")
        if duration is not None:
            dtend_val = dtstart_val + duration.dt
        elif all_day:
            dtend_val = dtstart_val + timedelta(days=1)
        else:
            dtend_val = dtstart_val

    organizer_prop = component.get("ORGANIZER")
    organizer = (
        Organizer(email=_addr_email(organizer_prop), cn=_addr_cn(organizer_prop))
        if organizer_prop is not None
        else None
    )

    attendees = [
        Attendee(
            email=_addr_email(a), cn=_addr_cn(a),
            partstat=_partstat_of(a), role=_role_of(a),
        )
        for a in _as_list(component.get("ATTENDEE"))
    ]

    recurrence_id_prop = component.get("RECURRENCE-ID")
    recurrence_id = recurrence_id_prop.to_ical().decode("utf-8") if recurrence_id_prop else None

    status_raw = str(component.get("STATUS", "CONFIRMED")).lower()

    return ParsedEvent(
        uid=str(component.get("UID", "")),
        summary=str(component.get("SUMMARY", "")),
        dtstart=_to_datetime(dtstart_val, all_day),
        dtend=_to_datetime(dtend_val, all_day),
        tz=_tz_name(dtstart_val),
        all_day=all_day,
        location=str(component["LOCATION"]) if component.get("LOCATION") else None,
        description=str(component["DESCRIPTION"]) if component.get("DESCRIPTION") else None,
        status=status_raw if status_raw in ("confirmed", "tentative", "cancelled") else "confirmed",
        sequence=int(component.get("SEQUENCE", 0)),
        organizer=organizer,
        attendees=attendees,
        recurrence_id=recurrence_id,
        rrule=component.get("RRULE").to_ical().decode("utf-8") if component.get("RRULE") else None,
        is_recurring=is_recurring,
        is_exception=is_exception,
    )


def _components(data: str) -> list[Component]:
    cal = Calendar.from_ical(data)
    return [c for c in cal.walk() if c.name == "VEVENT"]


def get_method(data: str) -> str | None:
    """The METHOD property of a VCALENDAR body, or None if absent."""
    cal = Calendar.from_ical(data)
    method = cal.get("METHOD")
    return str(method) if method is not None else None


def get_uid(data: str) -> str:
    """The UID shared by the master and every exception in this body."""
    components = _components(data)
    if not components:
        raise ValueError("VCALENDAR has no VEVENT component")
    return str(components[0].get("UID", ""))


def parse_master_and_exceptions(data: str) -> tuple[ParsedEvent, list[ParsedEvent]]:
    """The master component (no RECURRENCE-ID) and every exception
    alongside it, as stored -- never expanded."""
    components = _components(data)
    if not components:
        raise ValueError("VCALENDAR has no VEVENT component")
    master_component = next((c for c in components if c.get("RECURRENCE-ID") is None), None)
    if master_component is None:
        raise ValueError("VCALENDAR has no master VEVENT (every component carries RECURRENCE-ID)")
    is_recurring = bool(master_component.get("RRULE") or master_component.get("RDATE"))
    master = _parse_component(master_component, is_recurring=is_recurring, is_exception=False)
    exceptions = [
        _parse_component(c, is_recurring=is_recurring, is_exception=True)
        for c in components
        if c.get("RECURRENCE-ID") is not None
    ]
    return master, exceptions


def parse_invitation(data: str) -> ParsedInvitation:
    """A whole invitation body: METHOD plus the master and its exceptions."""
    method = get_method(data)
    if method is None:
        raise ValueError("Invitation carries no METHOD")
    master, exceptions = parse_master_and_exceptions(data)
    return ParsedInvitation(method=method, master=master, exceptions=exceptions)


def expand_instances(data: str, window_start: datetime, window_end: datetime) -> list[ParsedEvent]:
    """
    Every occurrence between window_start and window_end, master and
    recurring alike -- recurring-ical-events resolves RRULE/RDATE/EXDATE
    and applies stored exceptions by RECURRENCE-ID itself, so a modified
    occurrence in the returned list already carries its exception's own
    fields (a moved time, a different summary) rather than the series
    default.

    Args:
        data: The whole VCALENDAR body (one UID)
        window_start: Inclusive start of the date range
        window_end: Exclusive end of the date range

    Returns:
        One ParsedEvent per occurrence in range, dtstart-ordered
    """
    cal = Calendar.from_ical(data)
    is_recurring = any(
        c.get("RRULE") or c.get("RDATE") for c in cal.walk() if c.name == "VEVENT"
    )
    occurrences = recurring_ical_events.of(cal).between(window_start, window_end)
    parsed = [
        _parse_component(
            occ, is_recurring=is_recurring,
            is_exception=occ.get("RECURRENCE-ID") is not None,
        )
        for occ in occurrences
    ]
    return sorted(parsed, key=lambda e: e.dtstart)


def strip_method(data: str) -> str:
    """Remove METHOD -- a calendar object stored on a server must not
    carry it (Nextcloud refuses it with 415; see the contract's "Do not
    store METHOD")."""
    cal = Calendar.from_ical(data)
    if "METHOD" in cal:
        del cal["METHOD"]
    return _serialize(cal)


def set_schedule_agent_client_on_organizer(data: str) -> str:
    """Silence a server's own scheduling engine when we hold an
    invitation as an attendee: the ORGANIZER line carries
    SCHEDULE-AGENT=CLIENT, so the server never tries to relay our
    PARTSTAT changes itself -- MailVerdict sends the REPLY."""
    cal = Calendar.from_ical(data)
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        organizer = component.get("ORGANIZER")
        if organizer is not None:
            organizer.params["SCHEDULE-AGENT"] = "CLIENT"
    return _serialize(cal)


def set_schedule_agent_client_on_attendees(data: str) -> str:
    """The organizer-side counterpart: every ATTENDEE carries
    SCHEDULE-AGENT=CLIENT, so the server never emails invitations or
    cancellations itself -- MailVerdict sends the REQUEST/CANCEL."""
    cal = Calendar.from_ical(data)
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        for attendee in _as_list(component.get("ATTENDEE")):
            attendee.params["SCHEDULE-AGENT"] = "CLIENT"
    return _serialize(cal)


def build_new_event(
    *,
    summary: str,
    dtstart: datetime,
    dtend: datetime,
    all_day: bool = False,
    location: str | None = None,
    description: str | None = None,
    rrule: str | None = None,
    organizer_email: str | None = None,
    organizer_cn: str | None = None,
    attendees: list[tuple[str, str | None]] | None = None,
) -> str:
    """
    Build a fresh VCALENDAR body for an event this application originates.

    Args:
        summary: Event title
        dtstart: Start time (or date-only day, given as midnight UTC, when all_day)
        dtend: End time (exclusive for all-day)
        all_day: Whether DTSTART/DTEND are dates rather than datetimes
        location: Location text
        description: Description text
        rrule: A raw RRULE value (e.g. "FREQ=WEEKLY;BYDAY=MO"), or None
        organizer_email: This calendar's identity, if attendees are given
        organizer_cn: Display name for the organizer
        attendees: (email, cn) pairs; SCHEDULE-AGENT=CLIENT is set on each
            so the server never sends its own invitations

    Returns:
        A new VCALENDAR body with a freshly generated UID
    """
    cal = Calendar()
    cal.add("VERSION", "2.0")
    cal.add("PRODID", "-//MailVerdict//Calendar//EN")

    event = Event()
    event.add("UID", str(uuid.uuid4()))
    event.add("DTSTAMP", datetime.now(timezone.utc))
    event.add("SUMMARY", summary)
    event.add("DTSTART", dtstart.date() if all_day else dtstart)
    event.add("DTEND", dtend.date() if all_day else dtend)
    event.add("SEQUENCE", 0)
    if location:
        event.add("LOCATION", location)
    if description:
        event.add("DESCRIPTION", description)
    if rrule:
        event.add("RRULE", _parse_rrule_value(rrule))

    if attendees:
        if not organizer_email:
            raise ValueError("An event with attendees needs an organizer")
        organizer = vCalAddress(f"mailto:{organizer_email}")
        if organizer_cn:
            organizer.params["CN"] = vText(organizer_cn)
        event["ORGANIZER"] = organizer
        for email, cn in attendees:
            attendee = vCalAddress(f"mailto:{email}")
            attendee.params["ROLE"] = vText("REQ-PARTICIPANT")
            attendee.params["PARTSTAT"] = vText("NEEDS-ACTION")
            attendee.params["RSVP"] = vText("TRUE")
            attendee.params["SCHEDULE-AGENT"] = vText("CLIENT")
            if cn:
                attendee.params["CN"] = vText(cn)
            event.add("ATTENDEE", attendee, encode=False)

    cal.add_component(event)
    return _serialize(cal)


def _parse_rrule_value(rrule: str) -> dict[str, Any]:
    """RRULE as icalendar's `vRecur.from_ical` expects: a
    "FREQ=WEEKLY;BYDAY=MO" string, not the "RRULE:" prefix."""
    from icalendar import vRecur

    return cast(dict[str, Any], vRecur.from_ical(rrule))


def replace_master_fields(
    data: str,
    *,
    summary: str | None = None,
    dtstart: datetime | None = None,
    dtend: datetime | None = None,
    all_day: bool | None = None,
    location: str | None = None,
    description: str | None = None,
    rrule: str | None = None,
) -> str:
    """
    Edit the master VEVENT in place -- scope="all" on a recurring series,
    or the only edit path for a non-recurring event. Fields left as None
    are unchanged; SEQUENCE is bumped, which is what tells an external
    organizer's calendar this is a newer version.
    """
    cal = Calendar.from_ical(data)
    master = next(
        (c for c in cal.walk() if c.name == "VEVENT" and c.get("RECURRENCE-ID") is None), None,
    )
    if master is None:
        raise ValueError("VCALENDAR has no master VEVENT")

    if summary is not None:
        master["SUMMARY"] = summary
    if dtstart is not None:
        is_all_day = all_day if all_day is not None else not isinstance(
            master.get("DTSTART").dt, datetime,
        )
        master["DTSTART"] = dtstart.date() if is_all_day else dtstart
    if dtend is not None:
        is_all_day = all_day if all_day is not None else not isinstance(
            master.get("DTSTART").dt, datetime,
        )
        master["DTEND"] = dtend.date() if is_all_day else dtend
    if location is not None:
        master["LOCATION"] = location
    if description is not None:
        master["DESCRIPTION"] = description
    if rrule is not None:
        master["RRULE"] = _parse_rrule_value(rrule) if rrule else None
        if not rrule and "RRULE" in master:
            del master["RRULE"]

    master["SEQUENCE"] = int(master.get("SEQUENCE", 0)) + 1
    return _serialize(cal)


def set_partstat(
    data: str, attendee_email: str, partstat: str, *, recurrence_id: str | None = None,
) -> str:
    """
    Update one attendee's PARTSTAT on the master (recurrence_id=None) or
    on the exception matching recurrence_id -- responding to an
    invitation.
    """
    cal = Calendar.from_ical(data)
    target = _find_component(cal, recurrence_id)
    attendee_lower = attendee_email.lower()
    found = False
    for attendee in _as_list(target.get("ATTENDEE")):
        if _addr_email(attendee).lower() == attendee_lower:
            attendee.params["PARTSTAT"] = partstat.upper()
            found = True
    if not found:
        raise ValueError(f"{attendee_email} is not an attendee of this event")
    return _serialize(cal)


def _recurrence_id_of(component: Component) -> str | None:
    rid = component.get("RECURRENCE-ID")
    return _serialize(rid) if rid else None


def _find_component(cal: Calendar, recurrence_id: str | None) -> Component:
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        if _recurrence_id_of(component) == recurrence_id:
            return component
    raise ValueError(
        "No matching VEVENT" if recurrence_id is None
        else f"No VEVENT with RECURRENCE-ID={recurrence_id}"
    )


def mark_cancelled(data: str, *, recurrence_id: str | None = None) -> str:
    """Set STATUS:CANCELLED on the whole series (recurrence_id=None) or
    on one occurrence -- the event stays visible, cancelled, until the
    user removes it (the intake design's CANCEL handling)."""
    cal = Calendar.from_ical(data)
    target = _find_component(cal, recurrence_id)
    target["STATUS"] = "CANCELLED"
    target["SEQUENCE"] = int(target.get("SEQUENCE", 0)) + 1
    return _serialize(cal)


def merge_exception(existing_data: str, exception_data: str, recurrence_id: str) -> str:
    """
    Merge an incoming RECURRENCE-ID component into an already-held
    series, replacing an existing exception with the same RECURRENCE-ID.
    """
    existing = Calendar.from_ical(existing_data)
    incoming = Calendar.from_ical(exception_data)
    incoming_component = next(
        (c for c in incoming.walk() if c.name == "VEVENT"), None,
    )
    if incoming_component is None:
        raise ValueError("Incoming body has no VEVENT to merge")

    # Rebuild rather than mutate in place: icalendar's Calendar has no
    # remove-component-by-identity, only subcomponents list surgery.
    existing.subcomponents = [
        c for c in existing.subcomponents
        if not (c.name == "VEVENT" and _recurrence_id_of(c) == recurrence_id)
    ]
    existing.add_component(incoming_component)
    return _serialize(existing)


def replace_exception_partstat_or_add(
    existing_data: str, master_organizer_email: str, attendee_email: str,
    partstat: str, recurrence_id: str,
) -> str:
    """REPLY intake for one occurrence of a series: update the exception's
    PARTSTAT if one already exists for recurrence_id, otherwise clone the
    master as a new exception carrying the updated PARTSTAT."""
    cal = Calendar.from_ical(existing_data)
    existing_exception = next(
        (
            c for c in cal.walk()
            if c.name == "VEVENT" and _recurrence_id_of(c) == recurrence_id
        ),
        None,
    )
    if existing_exception is not None:
        return set_partstat(
            cal.to_ical().decode("utf-8"), attendee_email, partstat, recurrence_id=recurrence_id,
        )

    master = next(
        (c for c in cal.walk() if c.name == "VEVENT" and c.get("RECURRENCE-ID") is None), None,
    )
    if master is None:
        raise ValueError("VCALENDAR has no master VEVENT")
    from copy import deepcopy

    exception = deepcopy(master)
    exception["RECURRENCE-ID"] = master.get("DTSTART")
    for attendee in _as_list(exception.get("ATTENDEE")):
        if _addr_email(attendee).lower() == attendee_email.lower():
            attendee.params["PARTSTAT"] = partstat.upper()
    cal.add_component(exception)
    return _serialize(cal)


def build_reply_ics(
    data: str, *, attendee_email: str, partstat: str,
    comment: str | None = None, recurrence_id: str | None = None,
) -> str:
    """
    Build a METHOD:REPLY VCALENDAR: only the ORGANIZER and this one
    ATTENDEE, sent over the identity's own outbox rather than left to the
    server's scheduling engine (which is silenced by
    SCHEDULE-AGENT=CLIENT on every object this application stores).
    """
    source = Calendar.from_ical(data)
    component = _find_component(source, recurrence_id)
    organizer_prop = component.get("ORGANIZER")
    if organizer_prop is None:
        raise ValueError("Event has no ORGANIZER to reply to")

    reply = Calendar()
    reply.add("VERSION", "2.0")
    reply.add("PRODID", "-//MailVerdict//Calendar//EN")
    reply.add("METHOD", "REPLY")

    event = Event()
    event.add("UID", component.get("UID"))
    event.add("DTSTAMP", datetime.now(timezone.utc))
    event.add("SEQUENCE", component.get("SEQUENCE", 0))
    if recurrence_id is not None:
        event["RECURRENCE-ID"] = component.get("RECURRENCE-ID")
    event["ORGANIZER"] = organizer_prop

    attendee = vCalAddress(f"mailto:{attendee_email}")
    attendee.params["PARTSTAT"] = vText(partstat.upper())
    if comment:
        event.add("COMMENT", comment)
    event.add("ATTENDEE", attendee, encode=False)

    reply.add_component(event)
    return _serialize(reply)
