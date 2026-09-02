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
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, cast

import recurring_ical_events
from icalendar import Calendar, Component, Event, vCalAddress, vDDDTypes, vText

# A window this wide could contain more occurrences than a caller ever
# means to render, and expand_instances() refuses rather than asking
# recurring-ical-events to actually generate them -- see
# TooManyOccurrencesError.
MAX_EXPANDED_OCCURRENCES = 3000

# The shortest possible gap, in seconds, between two occurrences at each
# RRULE FREQ -- deliberately conservative (a month is never shorter than
# 28 days, a year never shorter than 365), since BYDAY/BYSETPOS and
# similar only ever narrow a series further, never widen it. Used to
# reject a series before asking recurring-ical-events to expand it, not
# to size the result.
_FREQ_MIN_SECONDS = {
    "SECONDLY": 1,
    "MINUTELY": 60,
    "HOURLY": 3600,
    "DAILY": 86400,
    "WEEKLY": 7 * 86400,
    "MONTHLY": 28 * 86400,
    "YEARLY": 365 * 86400,
}


class TooManyOccurrencesError(ValueError):
    """A series whose RRULE could produce more than MAX_EXPANDED_OCCURRENCES
    occurrences inside the requested window -- raised before
    recurring-ical-events is ever asked to expand it, since that call
    itself is what a FREQ=SECONDLY series over even a one-day window
    measures at tens of seconds and hundreds of MB, synchronously, on the
    process's only event loop."""


def _would_exceed_window(
    component: Component, window_start: datetime, window_end: datetime,
) -> bool:
    """A cheap upper bound on how many occurrences this component's RRULE
    could produce inside [window_start, window_end) -- window duration
    divided by the shortest possible gap at this FREQ/INTERVAL. Never
    calls recurring-ical-events, since that call is the one this exists
    to guard; a component with no RRULE, or an unrecognised FREQ, is
    never flagged here."""
    rrule_prop = component.get("RRULE")
    if rrule_prop is None:
        return False
    freq_values = rrule_prop.get("FREQ")
    freq = str(freq_values[0]).upper() if freq_values else ""
    min_seconds = _FREQ_MIN_SECONDS.get(freq)
    if min_seconds is None:
        return False
    interval_values = rrule_prop.get("INTERVAL")
    interval = int(interval_values[0]) if interval_values else 1
    window_seconds = (window_end - window_start).total_seconds()
    if window_seconds <= 0:
        return False
    max_possible = window_seconds / (min_seconds * max(interval, 1))
    return max_possible > MAX_EXPANDED_OCCURRENCES


def validate_rrule_frequency(rrule: str) -> None:
    """Refuse FREQ=SECONDLY or FREQ=MINUTELY -- expand_instances()'s
    MAX_EXPANDED_OCCURRENCES guard already stops either from freezing a
    read, but an event this application originates has no reason to ever
    carry one, and rejecting it at the point of creation or edit is
    cheaper than storing an object no view will ever fully render."""
    parsed = _parse_rrule_value(rrule)
    freq_values = parsed.get("FREQ")
    freq = str(freq_values[0]).upper() if freq_values else ""
    if freq in ("SECONDLY", "MINUTELY"):
        raise ValueError(f"FREQ={freq} recurs too frequently to be usable")


def recurrence_id_to_datetime(recurrence_id: str) -> datetime:
    """The occurrence datetime a RECURRENCE-ID string names. RFC 5545
    ties every RECURRENCE-ID to the DTSTART of the occurrence it
    overrides, so this is where that exact occurrence would be found --
    used to search a window of hours around one specific occurrence
    instead of the whole series when looking one up by RECURRENCE-ID."""
    value = vDDDTypes.from_ical(recurrence_id)
    return _to_datetime(value, not isinstance(value, datetime))


def _serialize(component: Any) -> str:
    """icalendar's to_ical()/decode() chain is untyped -- this is the one
    place that tells mypy the result is the str every function here
    promises to return."""
    return cast(str, component.to_ical().decode("utf-8"))


def _parse_calendar(data: str) -> Calendar:
    """Calendar.from_ical() is inherited from Component and its stub
    returns Component even when called on Calendar -- this is the one
    place that casts back to what every VCALENDAR parse here actually
    is."""
    return cast(Calendar, Calendar.from_ical(data))


def _set(component: Component, name: str, value: Any) -> None:
    """
    Replace a singular property with a properly-typed value.

    `component[name] = value` (dict-style) stores whatever it's given
    completely unencoded -- a raw datetime has no .to_ical() of its own,
    so a later direct .to_ical() read (or even the whole calendar's own
    serialization) renders it with Python's str() instead of the RFC 5545
    wire format. `component.add(name, value)` does encode correctly, but
    never replaces -- called twice it leaves two properties of the same
    name. This does both: drop whatever is there, then add the encoded
    replacement.
    """
    if name in component:
        del component[name]
    component.add(name, value)


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
    component: Component, *, is_recurring: bool, is_exception: bool, lenient: bool = False,
) -> ParsedEvent:
    dtstart_prop = component.get("DTSTART")
    if dtstart_prop is None:
        if not lenient:
            raise ValueError("VEVENT has no DTSTART")
        # A REPLY commonly omits DTSTART (RFC 5546 does not require it) --
        # DTSTAMP is required on every component, and stands in.
        dtstamp_prop = component.get("DTSTAMP")
        dtstart_val: Any = (
            dtstamp_prop.dt if dtstamp_prop is not None else datetime.now(timezone.utc)
        )
    else:
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
    cal = _parse_calendar(data)
    return [c for c in cal.walk() if c.name == "VEVENT"]


def get_method(data: str) -> str | None:
    """The METHOD property of a VCALENDAR body, or None if absent."""
    cal = _parse_calendar(data)
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


def parse_itip_message(data: str) -> ParsedInvitation:
    """
    A raw iTIP message lifted out of an email attachment -- REQUEST, REPLY
    or CANCEL, for calendar/intake.py. parse_invitation() (and
    parse_master_and_exceptions() underneath it) is for a stored resource
    and is stricter than a message on the wire is guaranteed to be: a
    REPLY commonly omits DTSTART/DTEND/SUMMARY (RFC 5546 does not require
    them), and a REQUEST or CANCEL about a single occurrence of a series
    can arrive as a lone RECURRENCE-ID component with no master alongside
    it -- something parse_master_and_exceptions cannot represent at all,
    since it requires finding one.

    Whichever component appears first becomes "master" here regardless of
    whether it carries RECURRENCE-ID -- unlike the stored-resource
    functions, this reads exactly what the message says about itself
    rather than assuming it holds a whole series.
    """
    method = get_method(data)
    if method is None:
        raise ValueError("Invitation carries no METHOD")
    components = _components(data)
    if not components:
        raise ValueError("VCALENDAR has no VEVENT component")
    is_recurring = bool(components[0].get("RRULE") or components[0].get("RDATE"))
    master = _parse_component(
        components[0], is_recurring=is_recurring,
        is_exception=components[0].get("RECURRENCE-ID") is not None, lenient=True,
    )
    exceptions = [
        _parse_component(c, is_recurring=is_recurring, is_exception=True, lenient=True)
        for c in components[1:]
    ]
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

    Raises:
        TooManyOccurrencesError: the RRULE could produce more than
            MAX_EXPANDED_OCCURRENCES occurrences in this window -- never
            actually attempted, see _would_exceed_window().
    """
    cal = _parse_calendar(data)
    vevents = [c for c in cal.walk() if c.name == "VEVENT"]
    if any(_would_exceed_window(c, window_start, window_end) for c in vevents):
        raise TooManyOccurrencesError(
            f"RRULE would produce more than {MAX_EXPANDED_OCCURRENCES} occurrences "
            f"between {window_start.isoformat()} and {window_end.isoformat()}",
        )
    is_recurring = any(c.get("RRULE") or c.get("RDATE") for c in vevents)
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
    cal = _parse_calendar(data)
    if "METHOD" in cal:
        del cal["METHOD"]
    return _serialize(cal)


def with_method(data: str, method: str) -> str:
    """The inverse of strip_method() -- for the .ics attachment mailed
    alongside a REQUEST/CANCEL, which does need METHOD, unlike the copy
    this application stores."""
    cal = _parse_calendar(data)
    _set(cal, "METHOD", method)
    return _serialize(cal)


def set_schedule_agent_client_on_organizer(data: str) -> str:
    """Silence a server's own scheduling engine when we hold an
    invitation as an attendee: the ORGANIZER line carries
    SCHEDULE-AGENT=CLIENT, so the server never tries to relay our
    PARTSTAT changes itself -- MailVerdict sends the REPLY."""
    cal = _parse_calendar(data)
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
    cal = _parse_calendar(data)
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
        validate_rrule_frequency(rrule)
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


def _apply_field_overrides(
    component: Component,
    *,
    summary: str | None,
    dtstart: datetime | None,
    dtend: datetime | None,
    all_day: bool | None,
    location: str | None,
    description: str | None,
    rrule: str | None,
    bump_sequence: bool,
) -> None:
    """Mutate one VEVENT component in place -- fields left as None are
    unchanged. Shared by replace_master_fields() (scope="all") and
    edit_occurrence() (scope="this")."""
    if summary is not None:
        _set(component, "SUMMARY", summary)
    if dtstart is not None:
        is_all_day = all_day if all_day is not None else not isinstance(
            component.get("DTSTART").dt, datetime,
        )
        _set(component, "DTSTART", dtstart.date() if is_all_day else dtstart)
    if dtend is not None:
        is_all_day = all_day if all_day is not None else not isinstance(
            component.get("DTSTART").dt, datetime,
        )
        _set(component, "DTEND", dtend.date() if is_all_day else dtend)
    if location is not None:
        _set(component, "LOCATION", location)
    if description is not None:
        _set(component, "DESCRIPTION", description)
    if rrule is not None:
        if "RRULE" in component:
            del component["RRULE"]
        if rrule:
            validate_rrule_frequency(rrule)
            component.add("RRULE", _parse_rrule_value(rrule))
    if bump_sequence:
        _set(component, "SEQUENCE", int(component.get("SEQUENCE", 0)) + 1)


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
    bump_sequence: bool = True,
) -> str:
    """
    Edit the master VEVENT in place -- scope="all" on a recurring series,
    or the only edit path for a non-recurring event. Fields left as None
    are unchanged.

    SEQUENCE is the ORGANIZER's own version counter (RFC 5545) --
    bump_sequence defaults to True for a caller that already knows this
    calendar organizes the event, and must be passed False otherwise:
    advancing SEQUENCE on an event held only as an attendee makes the
    next genuine update from the real organizer look stale by comparison
    to calendar/intake.py's own staleness check, and lose silently.
    """
    cal = _parse_calendar(data)
    master = next(
        (c for c in cal.walk() if c.name == "VEVENT" and c.get("RECURRENCE-ID") is None), None,
    )
    if master is None:
        raise ValueError("VCALENDAR has no master VEVENT")

    _apply_field_overrides(
        master, summary=summary, dtstart=dtstart, dtend=dtend, all_day=all_day,
        location=location, description=description, rrule=rrule, bump_sequence=bump_sequence,
    )
    return _serialize(cal)


def _get_or_clone_exception(cal: Calendar, recurrence_id: str) -> Component:
    """
    The exception component at recurrence_id, creating it first if the
    series has none there yet -- the common case, since a occurrence a
    caller names almost always came from expand_instances() rather than
    from a component actually stored in `data`. Cloned from the master,
    with RRULE dropped (an exception does not recur its own series) and
    RECURRENCE-ID set to the occurrence actually named -- never the
    master's own DTSTART, which is the series' first occurrence and
    would silently misfile every later one.
    """
    existing = next(
        (c for c in cal.walk() if c.name == "VEVENT" and _recurrence_id_of(c) == recurrence_id),
        None,
    )
    if existing is not None:
        return existing

    master = next(
        (c for c in cal.walk() if c.name == "VEVENT" and c.get("RECURRENCE-ID") is None), None,
    )
    if master is None:
        raise ValueError("VCALENDAR has no master VEVENT")
    exception = deepcopy(master)
    if "RRULE" in exception:
        del exception["RRULE"]
    _set(exception, "RECURRENCE-ID", vDDDTypes.from_ical(recurrence_id))
    cal.add_component(exception)
    return exception


def edit_occurrence(
    data: str,
    recurrence_id: str,
    *,
    summary: str | None = None,
    dtstart: datetime | None = None,
    dtend: datetime | None = None,
    all_day: bool | None = None,
    location: str | None = None,
    description: str | None = None,
    bump_sequence: bool = True,
) -> str:
    """
    scope="this": edit one occurrence of a series without touching the
    others. Updates the existing exception at recurrence_id if there is
    one, otherwise clones the master as a new exception carrying the
    overrides. See replace_master_fields() for what bump_sequence gates
    and why it must be False for anything held only as an attendee.
    """
    cal = _parse_calendar(data)
    target = _get_or_clone_exception(cal, recurrence_id)
    _apply_field_overrides(
        target, summary=summary, dtstart=dtstart, dtend=dtend, all_day=all_day,
        location=location, description=description, rrule=None, bump_sequence=bump_sequence,
    )
    return _serialize(cal)


def set_partstat(
    data: str, attendee_email: str, partstat: str, *, recurrence_id: str | None = None,
) -> str:
    """
    Update one attendee's PARTSTAT on the master (recurrence_id=None) or
    on the exception matching recurrence_id -- responding to an
    invitation.
    """
    cal = _parse_calendar(data)
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


def mark_cancelled(
    data: str, *, recurrence_id: str | None = None, bump_sequence: bool = True,
) -> str:
    """Set STATUS:CANCELLED on the whole series (recurrence_id=None) or
    on one occurrence -- the event stays visible, cancelled, until the
    user removes it (the intake design's CANCEL handling). Cancelling one
    occurrence of a series that has no stored exception there yet clones
    the master as a cancelled exception, the same as edit_occurrence().
    See replace_master_fields() for what bump_sequence gates."""
    cal = _parse_calendar(data)
    target = (
        _get_or_clone_exception(cal, recurrence_id)
        if recurrence_id is not None
        else _find_component(cal, None)
    )
    _set(target, "STATUS", "CANCELLED")
    if bump_sequence:
        _set(target, "SEQUENCE", int(target.get("SEQUENCE", 0)) + 1)
    return _serialize(cal)


def merge_exception(existing_data: str, exception_data: str, recurrence_id: str) -> str:
    """
    Merge an incoming RECURRENCE-ID component into an already-held
    series, replacing an existing exception with the same RECURRENCE-ID.
    """
    existing = _parse_calendar(existing_data)
    incoming = _parse_calendar(exception_data)
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
    existing_data: str, attendee_email: str, partstat: str, recurrence_id: str,
) -> str:
    """REPLY intake for one occurrence of a series: update the exception's
    PARTSTAT if one already exists for recurrence_id, otherwise clone the
    master as a new exception carrying the updated PARTSTAT."""
    cal = _parse_calendar(existing_data)
    exception = _get_or_clone_exception(cal, recurrence_id)
    for attendee in _as_list(exception.get("ATTENDEE")):
        if _addr_email(attendee).lower() == attendee_email.lower():
            attendee.params["PARTSTAT"] = partstat.upper()
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
    source = _parse_calendar(data)
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
