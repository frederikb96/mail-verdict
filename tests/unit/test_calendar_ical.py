"""calendar/ical.py -- parsing, building and editing VEVENT bodies. No
database and no network: every case here is a plain string in, string or
dataclass out."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from icalendar import Calendar, Component, Event

from mail_verdict.calendar import ical


def _unfolded(data: str) -> str:
    """Undo RFC 5545 line folding (a continuation line starts with a
    single space) so a substring check isn't fooled by a parameter
    landing on the wrapped part of a long line."""
    return data.replace("\r\n ", "").replace("\n ", "")

_SIMPLE_EVENT = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Test//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:event-1@example.com\r\n"
    "DTSTAMP:20260901T120000Z\r\n"
    "DTSTART:20260905T140000Z\r\n"
    "DTEND:20260905T150000Z\r\n"
    "SUMMARY:Team sync\r\n"
    "LOCATION:Room 4\r\n"
    "SEQUENCE:0\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)

_INVITATION_REQUEST = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Test//EN\r\n"
    "METHOD:REQUEST\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:invite-1@example.com\r\n"
    "DTSTAMP:20260901T120000Z\r\n"
    "DTSTART:20260910T090000Z\r\n"
    "DTEND:20260910T100000Z\r\n"
    "SUMMARY:Kickoff\r\n"
    "SEQUENCE:0\r\n"
    "ORGANIZER;CN=Anna Mueller:mailto:anna@example.com\r\n"
    "ATTENDEE;CN=Freddy;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;"
    "RSVP=TRUE:mailto:freddy@work.example\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)

_RECURRING_EVENT = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Test//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:series-1@example.com\r\n"
    "DTSTAMP:20260901T120000Z\r\n"
    "DTSTART:20260901T090000Z\r\n"
    "DTEND:20260901T100000Z\r\n"
    "SUMMARY:Weekly standup\r\n"
    "SEQUENCE:0\r\n"
    "RRULE:FREQ=WEEKLY;COUNT=4\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:series-1@example.com\r\n"
    "RECURRENCE-ID:20260908T090000Z\r\n"
    "DTSTAMP:20260901T120000Z\r\n"
    "DTSTART:20260908T110000Z\r\n"
    "DTEND:20260908T120000Z\r\n"
    "SUMMARY:Weekly standup (moved)\r\n"
    "SEQUENCE:1\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


# A body shaped like what a real CalDAV server (Nextcloud, or an Outlook
# invitation) actually emits for a recurring series with a timezone-bound
# repeat: a VTIMEZONE component, an RRULE narrowed by two EXDATE
# properties (one holding a single date, one holding a comma-separated
# pair -- both forms RFC 5545 allows), an RDATE adding an extra one-off
# occurrence, an exception overriding one occurrence, and a scattering of
# properties this codebase has never modeled (CATEGORIES, CLASS, TRANSP,
# an X- property, a VALARM subcomponent). Row 125's round-trip proof: none
# of this may be narrowed, dropped or rewritten by an edit that only
# means to change one field.
_EXOTIC_RECURRING_EVENT = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Test//EN\r\n"
    "X-WR-CALNAME:Personal\r\n"
    "BEGIN:VTIMEZONE\r\n"
    "TZID:Europe/Berlin\r\n"
    "X-LIC-LOCATION:Europe/Berlin\r\n"
    "BEGIN:DAYLIGHT\r\n"
    "TZOFFSETFROM:+0100\r\n"
    "TZOFFSETTO:+0200\r\n"
    "TZNAME:CEST\r\n"
    "DTSTART:19700329T020000\r\n"
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\r\n"
    "END:DAYLIGHT\r\n"
    "BEGIN:STANDARD\r\n"
    "TZOFFSETFROM:+0200\r\n"
    "TZOFFSETTO:+0100\r\n"
    "TZNAME:CET\r\n"
    "DTSTART:19701025T030000\r\n"
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\r\n"
    "END:STANDARD\r\n"
    "END:VTIMEZONE\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:exotic-1@example.com\r\n"
    "DTSTAMP:20260901T120000Z\r\n"
    "DTSTART;TZID=Europe/Berlin:20260907T090000\r\n"
    "DTEND;TZID=Europe/Berlin:20260907T100000\r\n"
    "SUMMARY:Weekly standup\r\n"
    "DESCRIPTION:Sync with the whole team\r\n"
    "LOCATION:Room 4\r\n"
    "CATEGORIES:WORK,PLANNING\r\n"
    "CLASS:PRIVATE\r\n"
    "TRANSP:OPAQUE\r\n"
    "SEQUENCE:0\r\n"
    "X-CUSTOM-CLIENT-ID:abc-123\r\n"
    "RRULE:FREQ=WEEKLY;COUNT=6\r\n"
    "EXDATE;TZID=Europe/Berlin:20260914T090000\r\n"
    "EXDATE;TZID=Europe/Berlin:20260928T090000,20261005T090000\r\n"
    "RDATE;TZID=Europe/Berlin:20261020T130000\r\n"
    "BEGIN:VALARM\r\n"
    "ACTION:DISPLAY\r\n"
    "DESCRIPTION:Reminder\r\n"
    "TRIGGER:-PT15M\r\n"
    "END:VALARM\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:exotic-1@example.com\r\n"
    "RECURRENCE-ID;TZID=Europe/Berlin:20260921T090000\r\n"
    "DTSTAMP:20260901T120000Z\r\n"
    "DTSTART;TZID=Europe/Berlin:20260921T100000\r\n"
    "DTEND;TZID=Europe/Berlin:20260921T110000\r\n"
    "SUMMARY:Weekly standup (moved)\r\n"
    "CATEGORIES:WORK,PLANNING\r\n"
    "SEQUENCE:1\r\n"
    "X-CUSTOM-CLIENT-ID:abc-123\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


class TestParsing:
    def test_parses_summary_and_times(self) -> None:
        master, exceptions = ical.parse_master_and_exceptions(_SIMPLE_EVENT)
        assert master.uid == "event-1@example.com"
        assert master.summary == "Team sync"
        assert master.location == "Room 4"
        assert master.dtstart == datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc)
        assert master.dtend == datetime(2026, 9, 5, 15, 0, tzinfo=timezone.utc)
        assert master.all_day is False
        assert master.is_recurring is False
        assert exceptions == []

    def test_parses_organizer_and_attendees(self) -> None:
        invitation = ical.parse_invitation(_INVITATION_REQUEST)
        assert invitation.method == "REQUEST"
        assert invitation.master.organizer is not None
        assert invitation.master.organizer.email == "anna@example.com"
        assert invitation.master.organizer.cn == "Anna Mueller"
        assert len(invitation.master.attendees) == 1
        attendee = invitation.master.attendees[0]
        assert attendee.email == "freddy@work.example"
        assert attendee.partstat == "needs-action"

    def test_recurring_master_carries_exception(self) -> None:
        master, exceptions = ical.parse_master_and_exceptions(_RECURRING_EVENT)
        assert master.is_recurring is True
        assert master.rrule == "FREQ=WEEKLY;COUNT=4"
        assert len(exceptions) == 1
        assert exceptions[0].is_exception is True
        assert exceptions[0].summary == "Weekly standup (moved)"


class TestExpansion:
    def test_expands_four_weekly_occurrences(self) -> None:
        instances = ical.expand_instances(
            _RECURRING_EVENT,
            datetime(2026, 8, 1, tzinfo=timezone.utc),
            datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        assert len(instances) == 4
        # The second occurrence was moved by the exception -- 11:00, not 09:00.
        assert instances[1].dtstart == datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc)
        assert instances[1].summary == "Weekly standup (moved)"

    def test_window_outside_the_series_returns_nothing(self) -> None:
        instances = ical.expand_instances(
            _RECURRING_EVENT,
            datetime(2027, 1, 1, tzinfo=timezone.utc),
            datetime(2027, 2, 1, tzinfo=timezone.utc),
        )
        assert instances == []


class TestOccurrenceBound:
    """Row 108: an event whose RRULE could expand to an unreasonable
    occurrence count inside the requested window is refused outright,
    before recurring-ical-events is ever asked to generate anything."""

    def test_secondly_over_a_day_is_refused(self) -> None:
        data = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//Test//EN\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:bomb-1@example.com\r\n"
            "DTSTAMP:20260901T120000Z\r\n"
            "DTSTART:20260901T000000Z\r\n"
            "DTEND:20260901T000001Z\r\n"
            "SUMMARY:Tick\r\n"
            "SEQUENCE:0\r\n"
            "RRULE:FREQ=SECONDLY\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        with pytest.raises(ical.TooManyOccurrencesError):
            ical.expand_instances(
                data,
                datetime(2026, 9, 1, tzinfo=timezone.utc),
                datetime(2026, 9, 2, tzinfo=timezone.utc),
            )

    def test_minutely_over_a_month_is_refused(self) -> None:
        """The finding's second amplifier: list_events' own calendar-month
        window, over a frequency the day-scale case alone would not
        already cover the reasoning for."""
        data = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//Test//EN\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:bomb-2@example.com\r\n"
            "DTSTAMP:20260901T120000Z\r\n"
            "DTSTART:20260901T000000Z\r\n"
            "DTEND:20260901T000001Z\r\n"
            "SUMMARY:Tick\r\n"
            "SEQUENCE:0\r\n"
            "RRULE:FREQ=MINUTELY\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        with pytest.raises(ical.TooManyOccurrencesError):
            ical.expand_instances(
                data,
                datetime(2026, 9, 1, tzinfo=timezone.utc),
                datetime(2026, 10, 1, tzinfo=timezone.utc),
            )

    def test_weekly_series_is_unaffected(self) -> None:
        """A normal recurring event, however wide the window, stays well
        under the bound and expands as before."""
        instances = ical.expand_instances(
            _RECURRING_EVENT,
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        assert len(instances) == 4

    def test_build_new_event_refuses_secondly_rrule(self) -> None:
        with pytest.raises(ValueError, match="SECONDLY"):
            ical.build_new_event(
                summary="Tick", dtstart=datetime(2026, 9, 1, tzinfo=timezone.utc),
                dtend=datetime(2026, 9, 1, 0, 1, tzinfo=timezone.utc),
                rrule="FREQ=SECONDLY",
            )

    def test_build_new_event_accepts_a_normal_rrule(self) -> None:
        data = ical.build_new_event(
            summary="Standup", dtstart=datetime(2026, 9, 1, tzinfo=timezone.utc),
            dtend=datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc),
            rrule="FREQ=WEEKLY;BYDAY=MO",
        )
        master, _ = ical.parse_master_and_exceptions(data)
        assert master.rrule == "FREQ=WEEKLY;BYDAY=MO"

    def test_replace_master_fields_refuses_minutely_rrule(self) -> None:
        with pytest.raises(ValueError, match="MINUTELY"):
            ical.replace_master_fields(_SIMPLE_EVENT, rrule="FREQ=MINUTELY")

    def test_recurrence_id_to_datetime_round_trips(self) -> None:
        assert ical.recurrence_id_to_datetime("20260908T090000Z") == datetime(
            2026, 9, 8, 9, 0, tzinfo=timezone.utc,
        )

    def test_byhour_byminute_bysecond_widening_is_refused(self) -> None:
        """A re-verification found the frequency-text guard this replaced
        waved this through: FREQ=DAILY with BYHOUR/BYMINUTE/BYSECOND all
        enumerated is one occurrence per second wearing a DAILY hat --
        those parts widen a series when FREQ is coarser than they are,
        they do not narrow it. The bound now measures real output, so
        the RRULE's own spelling cannot walk around it."""
        data = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//Test//EN\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:bomb-3@example.com\r\n"
            "DTSTAMP:20260901T120000Z\r\n"
            "DTSTART:20260901T000000Z\r\n"
            "DTEND:20260901T000001Z\r\n"
            "SUMMARY:Tick\r\n"
            "SEQUENCE:0\r\n"
            "RRULE:FREQ=DAILY;BYHOUR=0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,"
            "20,21,22,23;BYMINUTE=0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,"
            "21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,"
            "46,47,48,49,50,51,52,53,54,55,56,57,58,59;BYSECOND=0,10,20,30,40,50\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        with pytest.raises(ical.TooManyOccurrencesError):
            ical.expand_instances(
                data,
                datetime(2026, 9, 1, tzinfo=timezone.utc),
                datetime(2026, 9, 2, tzinfo=timezone.utc),
            )

    def test_rdate_only_bomb_is_refused(self) -> None:
        """RDATE with no RRULE at all was never inspected by the old
        text-based guard -- the new bound has no such blind spot, since
        it measures what the library actually returns regardless of
        which mechanism produced it. One-per-second for two hours is
        well past the 3000-occurrence cap while staying a fraction of
        what an attacker's RDATE list could actually hold."""
        base = datetime(2026, 9, 1, tzinfo=timezone.utc)
        rdates = ",".join(
            (base + timedelta(seconds=offset)).strftime("%Y%m%dT%H%M%SZ")
            for offset in range(7200)
        )
        data = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//Test//EN\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:bomb-4@example.com\r\n"
            "DTSTAMP:20260901T120000Z\r\n"
            "DTSTART:20260901T000000Z\r\n"
            "DTEND:20260901T000001Z\r\n"
            "SUMMARY:Tick\r\n"
            "SEQUENCE:0\r\n"
            f"RDATE:{rdates}\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        with pytest.raises(ical.TooManyOccurrencesError):
            ical.expand_instances(
                data,
                datetime(2026, 9, 1, tzinfo=timezone.utc),
                datetime(2026, 9, 2, tzinfo=timezone.utc),
            )

    def test_two_rrule_lines_do_not_crash(self) -> None:
        """A regression the old guard introduced: icalendar returns a
        list for a repeated property, and the old code called .get() on
        the RRULE value directly, raising AttributeError -- uncaught by
        list_events' `except ValueError`, so the whole month view 500'd.
        The bound never inspects RRULE text at all, so this is just two
        ordinary (harmless) rules to expand."""
        cal = Calendar()
        cal.add("VERSION", "2.0")
        cal.add("PRODID", "-//Test//EN")
        event = Event()
        event.add("UID", "two-rrules@example.com")
        event.add("DTSTAMP", datetime(2026, 9, 1, 12, tzinfo=timezone.utc))
        event.add("DTSTART", datetime(2026, 9, 1, tzinfo=timezone.utc))
        event.add("DTEND", datetime(2026, 9, 1, 1, tzinfo=timezone.utc))
        event.add("SUMMARY", "Double-ruled")
        event.add("SEQUENCE", 0)
        event.add("RRULE", {"FREQ": ["DAILY"]})
        event.add("RRULE", {"FREQ": ["WEEKLY"]})
        cal.add_component(event)
        data = cal.to_ical().decode("utf-8")

        # Must not raise AttributeError -- whatever recurring-ical-events
        # itself makes of two RRULE lines is between it and RFC 5545;
        # this only asserts the bound doesn't crash reading them.
        ical.expand_instances(
            data,
            datetime(2026, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 8, tzinfo=timezone.utc),
        )

    def test_count_over_a_wide_window_is_not_a_false_positive(self) -> None:
        """The old text-based guard ignored COUNT and UNTIL entirely, so
        a legitimate short-lived series over a wide window was refused
        and silently dropped by list_events. The bound measures real
        output, so ten real occurrences never trips a 3000-occurrence
        cap regardless of window width."""
        data = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//Test//EN\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:short-lived@example.com\r\n"
            "DTSTAMP:20260901T120000Z\r\n"
            "DTSTART:20260901T090000Z\r\n"
            "DTEND:20260901T093000Z\r\n"
            "SUMMARY:Daily check-in\r\n"
            "SEQUENCE:0\r\n"
            "RRULE:FREQ=MINUTELY;COUNT=10\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        instances = ical.expand_instances(
            data,
            datetime(2026, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        assert len(instances) == 10


class TestMethodAndScheduleAgent:
    def test_strip_method_removes_it(self) -> None:
        stripped = ical.strip_method(_INVITATION_REQUEST)
        assert ical.get_method(stripped) is None
        # The event body itself survives intact.
        master, _ = ical.parse_master_and_exceptions(stripped)
        assert master.summary == "Kickoff"

    def test_with_method_is_strip_methods_inverse(self) -> None:
        stripped = ical.strip_method(_INVITATION_REQUEST)
        restored = ical.with_method(stripped, "REQUEST")
        assert ical.get_method(restored) == "REQUEST"
        master, _ = ical.parse_master_and_exceptions(restored)
        assert master.summary == "Kickoff"

    def test_schedule_agent_client_on_organizer(self) -> None:
        updated = ical.set_schedule_agent_client_on_organizer(_INVITATION_REQUEST)
        assert "SCHEDULE-AGENT=CLIENT" in updated
        # It landed on the ORGANIZER line, not somewhere else.
        organizer_line = next(
            line for line in updated.splitlines() if line.startswith("ORGANIZER")
        )
        assert "SCHEDULE-AGENT=CLIENT" in organizer_line

    def test_schedule_agent_client_on_attendees(self) -> None:
        updated = ical.set_schedule_agent_client_on_attendees(_INVITATION_REQUEST)
        attendee_line = next(
            line for line in _unfolded(updated).splitlines() if line.startswith("ATTENDEE")
        )
        assert "SCHEDULE-AGENT=CLIENT" in attendee_line


class TestBuildAndEdit:
    def test_build_new_event_has_a_fresh_uid_and_zero_sequence(self) -> None:
        data = ical.build_new_event(
            summary="Planning",
            dtstart=datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
            dtend=datetime(2026, 9, 20, 11, 0, tzinfo=timezone.utc),
        )
        master, _ = ical.parse_master_and_exceptions(data)
        assert master.summary == "Planning"
        assert master.sequence == 0
        assert master.uid

    def test_build_new_event_with_attendees_sets_schedule_agent(self) -> None:
        data = ical.build_new_event(
            summary="Kickoff",
            dtstart=datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
            dtend=datetime(2026, 9, 20, 11, 0, tzinfo=timezone.utc),
            organizer_email="freddy@work.example",
            organizer_cn="Freddy Berg",
            attendees=[("anna@example.com", "Anna")],
        )
        master, _ = ical.parse_master_and_exceptions(data)
        assert master.organizer is not None
        assert master.organizer.email == "freddy@work.example"
        assert len(master.attendees) == 1
        assert master.attendees[0].email == "anna@example.com"
        assert "SCHEDULE-AGENT=CLIENT" in next(
            line for line in _unfolded(data).splitlines() if line.startswith("ATTENDEE")
        )

    def test_build_new_event_without_organizer_and_attendees_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="organizer"):
            ical.build_new_event(
                summary="Kickoff",
                dtstart=datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
                dtend=datetime(2026, 9, 20, 11, 0, tzinfo=timezone.utc),
                attendees=[("anna@example.com", "Anna")],
            )

    def test_replace_master_fields_bumps_sequence(self) -> None:
        updated = ical.replace_master_fields(_SIMPLE_EVENT, summary="Team sync (renamed)")
        master, _ = ical.parse_master_and_exceptions(updated)
        assert master.summary == "Team sync (renamed)"
        assert master.sequence == 1
        # Untouched fields survive.
        assert master.location == "Room 4"

    def test_replace_master_fields_with_bump_sequence_false_leaves_it(self) -> None:
        """Row 114: editing an event this calendar does not organize must
        not advance SEQUENCE -- it belongs to the organizer's own version
        counter (RFC 5545), and bumping it locally makes the organizer's
        next genuine update look stale by comparison."""
        updated = ical.replace_master_fields(
            _SIMPLE_EVENT, summary="Team sync (renamed)", bump_sequence=False,
        )
        master, _ = ical.parse_master_and_exceptions(updated)
        assert master.summary == "Team sync (renamed)"
        assert master.sequence == 0

    def test_edit_occurrence_with_bump_sequence_false_leaves_it(self) -> None:
        updated = ical.edit_occurrence(
            _RECURRING_EVENT, "20260901T090000Z",
            summary="Standup (renamed)", bump_sequence=False,
        )
        _master, exceptions = ical.parse_master_and_exceptions(updated)
        exception = next(e for e in exceptions if e.recurrence_id == "20260901T090000Z")
        assert exception.summary == "Standup (renamed)"
        assert exception.sequence == 0

    def test_mark_cancelled_with_bump_sequence_false_leaves_it(self) -> None:
        updated = ical.mark_cancelled(_SIMPLE_EVENT, bump_sequence=False)
        master, _ = ical.parse_master_and_exceptions(updated)
        assert master.status == "cancelled"
        assert master.sequence == 0

    def test_replace_master_fields_moves_the_time(self) -> None:
        """A raw datetime dict-assigned onto a Component serializes with
        Python's str() rather than the RFC 5545 wire format unless it goes
        through _set()'s encode step -- this is what catches a regression
        of that."""
        new_start = datetime(2026, 9, 6, 9, 0, tzinfo=timezone.utc)
        new_end = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
        updated = ical.replace_master_fields(_SIMPLE_EVENT, dtstart=new_start, dtend=new_end)
        assert "20260906T090000Z" in updated
        master, _ = ical.parse_master_and_exceptions(updated)
        assert master.dtstart == new_start
        assert master.dtend == new_end

    def test_set_partstat_updates_the_named_attendee(self) -> None:
        updated = ical.set_partstat(_INVITATION_REQUEST, "freddy@work.example", "accepted")
        master, _ = ical.parse_master_and_exceptions(updated)
        assert master.attendees[0].partstat == "accepted"

    def test_set_partstat_unknown_attendee_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="not an attendee"):
            ical.set_partstat(_INVITATION_REQUEST, "nobody@example.com", "accepted")

    def test_mark_cancelled_sets_status_and_bumps_sequence(self) -> None:
        updated = ical.mark_cancelled(_SIMPLE_EVENT)
        master, _ = ical.parse_master_and_exceptions(updated)
        assert master.status == "cancelled"
        assert master.sequence == 1

    def test_mark_cancelled_on_an_occurrence_with_no_stored_exception(self) -> None:
        """The named occurrence has never been edited before -- there is
        no exception component in `data` yet, only what recurring
        expansion computes. Cancelling it must clone one rather than
        failing to find it."""
        updated = ical.mark_cancelled(_RECURRING_EVENT, recurrence_id="20260915T090000Z")
        _, exceptions = ical.parse_master_and_exceptions(updated)
        cancelled = next(e for e in exceptions if e.recurrence_id == "20260915T090000Z")
        assert cancelled.status == "cancelled"
        # The master and the other exception are untouched.
        master, _ = ical.parse_master_and_exceptions(updated)
        assert master.status == "confirmed"


class TestReply:
    def test_build_reply_ics_carries_only_organizer_and_one_attendee(self) -> None:
        """A REPLY carries no DTSTART (RFC 5546) -- parse_master_and_exceptions
        is for a stored calendar object, not a one-off iTIP message, so
        this reads the built VEVENT directly."""
        from icalendar import Calendar

        reply = ical.build_reply_ics(
            _INVITATION_REQUEST, attendee_email="freddy@work.example", partstat="accepted",
        )
        assert ical.get_method(reply) == "REPLY"
        event = next(c for c in Calendar.from_ical(reply).walk() if c.name == "VEVENT")
        assert str(event["UID"]) == "invite-1@example.com"
        assert "mailto:anna@example.com" in str(event["ORGANIZER"]).lower()
        assert "mailto:freddy@work.example" in str(event["ATTENDEE"]).lower()
        assert str(event["ATTENDEE"].params["PARTSTAT"]) == "ACCEPTED"

    def test_build_reply_ics_without_organizer_raises(self) -> None:
        import pytest

        no_organizer = _SIMPLE_EVENT
        with pytest.raises(ValueError, match="ORGANIZER"):
            ical.build_reply_ics(no_organizer, attendee_email="x@example.com", partstat="accepted")


class TestExceptionMerge:
    def test_merge_exception_replaces_an_existing_one(self) -> None:
        replacement = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
            "UID:series-1@example.com\r\n"
            "RECURRENCE-ID:20260908T090000Z\r\n"
            "DTSTAMP:20260901T120000Z\r\n"
            "DTSTART:20260908T130000Z\r\n"
            "DTEND:20260908T140000Z\r\n"
            "SUMMARY:Weekly standup (moved again)\r\n"
            "SEQUENCE:2\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        merged = ical.merge_exception(_RECURRING_EVENT, replacement, "20260908T090000Z")
        _, exceptions = ical.parse_master_and_exceptions(merged)
        assert len(exceptions) == 1
        assert exceptions[0].summary == "Weekly standup (moved again)"


class TestEditOccurrence:
    def test_edits_an_existing_exception_in_place(self) -> None:
        updated = ical.edit_occurrence(
            _RECURRING_EVENT, "20260908T090000Z", summary="Standup (renamed)",
        )
        _, exceptions = ical.parse_master_and_exceptions(updated)
        assert len(exceptions) == 1
        assert exceptions[0].summary == "Standup (renamed)"
        # The other, untouched occurrences are unaffected.
        instances = ical.expand_instances(
            updated,
            datetime(2026, 8, 1, tzinfo=timezone.utc),
            datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        assert instances[0].summary == "Weekly standup"

    def test_creates_a_new_exception_at_the_named_occurrence(self) -> None:
        """The third occurrence (2026-09-15) has no exception yet -- this
        creates one carrying only the override, RECURRENCE-ID set to the
        occurrence actually named, not the master's own DTSTART."""
        updated = ical.edit_occurrence(
            _RECURRING_EVENT, "20260915T090000Z", summary="Standup (special)",
        )
        _, exceptions = ical.parse_master_and_exceptions(updated)
        assert len(exceptions) == 2
        new_exception = next(e for e in exceptions if e.summary == "Standup (special)")
        assert new_exception.recurrence_id == "20260915T090000Z"
        # The exception does not carry its own RRULE (it would recurse a
        # second series from one occurrence otherwise).
        assert new_exception.rrule is None


class TestParseItipMessage:
    """calendar/intake.py's own parser for a raw iTIP message -- more
    tolerant than parse_invitation()/parse_master_and_exceptions(), which
    are for a stored resource and assume it holds a whole series."""

    def test_parses_a_request_the_same_as_parse_invitation(self) -> None:
        parsed = ical.parse_itip_message(_INVITATION_REQUEST)
        assert parsed.method == "REQUEST"
        assert parsed.master.uid == "invite-1@example.com"
        assert parsed.master.recurrence_id is None
        assert parsed.exceptions == []

    def test_reply_with_no_dtstart_does_not_raise(self) -> None:
        """RFC 5546 does not require DTSTART on a REPLY --
        parse_invitation() raises on this; parse_itip_message() falls
        back to DTSTAMP instead."""
        reply = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "METHOD:REPLY\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:invite-1@example.com\r\n"
            "DTSTAMP:20260901T130000Z\r\n"
            "SEQUENCE:0\r\n"
            "ORGANIZER:mailto:anna@example.com\r\n"
            "ATTENDEE;PARTSTAT=ACCEPTED:mailto:freddy@work.example\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        parsed = ical.parse_itip_message(reply)
        assert parsed.method == "REPLY"
        assert parsed.master.uid == "invite-1@example.com"
        assert parsed.master.dtstart == datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc)
        assert parsed.master.attendees[0].partstat == "accepted"

    def test_a_lone_occurrence_with_no_master_does_not_raise(self) -> None:
        """A CANCEL or REQUEST about one occurrence of a series can arrive
        as a single RECURRENCE-ID component with no master alongside it
        -- parse_master_and_exceptions() has no master to find here and
        raises; parse_itip_message() takes the one component it gets."""
        one_occurrence = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "METHOD:CANCEL\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:series-1@example.com\r\n"
            "RECURRENCE-ID:20260908T090000Z\r\n"
            "DTSTAMP:20260901T120000Z\r\n"
            "DTSTART:20260908T110000Z\r\n"
            "DTEND:20260908T120000Z\r\n"
            "SUMMARY:Weekly standup (moved)\r\n"
            "SEQUENCE:1\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        parsed = ical.parse_itip_message(one_occurrence)
        assert parsed.method == "CANCEL"
        assert parsed.master.recurrence_id == "20260908T090000Z"
        assert parsed.exceptions == []

    def test_no_method_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="METHOD"):
            ical.parse_itip_message(_SIMPLE_EVENT)


def _vtimezones(data: str) -> list[Component]:
    return [c for c in Calendar.from_ical(data).walk() if c.name == "VTIMEZONE"]


def _master_component(data: str) -> Component:
    return next(
        c for c in Calendar.from_ical(data).walk()
        if c.name == "VEVENT" and c.get("RECURRENCE-ID") is None
    )


def _exception_components(data: str) -> list[Component]:
    return [
        c for c in Calendar.from_ical(data).walk()
        if c.name == "VEVENT" and c.get("RECURRENCE-ID") is not None
    ]


class TestPropertyPreservation:
    """Row 125: a complex object read from elsewhere must not be narrowed,
    dropped or rewritten by an edit that only means to change one field --
    everything _EXOTIC_RECURRING_EVENT carries that this codebase never
    models has to survive every edit path unchanged."""

    def test_the_exotic_body_expands_exactly_as_its_own_rules_say(self) -> None:
        """Functional proof that EXDATE, RDATE and the exception are all
        actually honoured, not just present in the text: two of the six
        RRULE occurrences are excluded (one from a single-date EXDATE, one
        from a comma-separated pair on a second EXDATE), one is moved by
        the exception, and RDATE adds a seventh instance outside the rule
        entirely."""
        instances = ical.expand_instances(
            _EXOTIC_RECURRING_EVENT,
            datetime(2026, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 11, 1, tzinfo=timezone.utc),
        )
        starts = sorted(i.dtstart for i in instances)
        assert starts == [
            datetime(2026, 9, 7, 7, 0, tzinfo=timezone.utc),   # 09:00 CEST
            datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc),  # moved to 10:00 CEST
            datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 20, 11, 0, tzinfo=timezone.utc),  # 13:00 CEST, from RDATE
        ]
        moved = next(i for i in instances if i.dtstart.day == 21)
        assert moved.summary == "Weekly standup (moved)"

    def test_replace_master_fields_preserves_everything_it_does_not_touch(self) -> None:
        updated = ical.replace_master_fields(_EXOTIC_RECURRING_EVENT, summary="Renamed standup")

        master, _ = ical.parse_master_and_exceptions(updated)
        assert master.summary == "Renamed standup"
        assert master.sequence == 1

        # The VTIMEZONE, the exception, and the calendar-level X- property
        # this codebase has never modeled all survive.
        assert len(_vtimezones(updated)) == 1
        assert str(_vtimezones(updated)[0]["TZID"]) == "Europe/Berlin"
        assert "X-WR-CALNAME:Personal" in _unfolded(updated)
        assert len(_exception_components(updated)) == 1

        component = _master_component(updated)
        assert "EXDATE" in component
        assert "RDATE" in component
        assert component.get("CATEGORIES").to_ical() == b"WORK,PLANNING"
        assert str(component.get("CLASS")) == "PRIVATE"
        assert str(component.get("TRANSP")) == "OPAQUE"
        assert str(component.get("X-CUSTOM-CLIENT-ID")) == "abc-123"
        assert [c.name for c in component.subcomponents] == ["VALARM"]

        # Excluding the two EXDATE-covered dates and honouring the moved
        # exception and the RDATE addition still holds after the edit.
        instances = ical.expand_instances(
            updated,
            datetime(2026, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 11, 1, tzinfo=timezone.utc),
        )
        assert len(instances) == 4
        assert not any(i.dtstart.month == 9 and i.dtstart.day in (14, 28) for i in instances)
        assert not any(i.dtstart.month == 10 and i.dtstart.day == 5 for i in instances)
        unmoved = next(i for i in instances if i.dtstart.day == 7)
        assert unmoved.summary == "Renamed standup"

    def test_edit_occurrence_preserves_the_master_and_the_other_properties(self) -> None:
        """scope=this touches only the exception at the named
        RECURRENCE-ID -- the master's own RRULE/EXDATE/RDATE/VTIMEZONE and
        the exception's own untouched fields must survive."""
        updated = ical.edit_occurrence(
            _EXOTIC_RECURRING_EVENT, "20260921T090000", summary="Standup (moved again)",
        )

        master, exceptions = ical.parse_master_and_exceptions(updated)
        assert master.summary == "Weekly standup"
        assert master.sequence == 0

        master_component = _master_component(updated)
        assert "EXDATE" in master_component
        assert "RDATE" in master_component
        assert len(_vtimezones(updated)) == 1

        assert len(exceptions) == 1
        assert exceptions[0].summary == "Standup (moved again)"
        # category (never touched by edit_occurrence) survives the edit.
        exception_component = _exception_components(updated)[0]
        assert exception_component.get("CATEGORIES").to_ical() == b"WORK,PLANNING"
        assert str(exception_component.get("X-CUSTOM-CLIENT-ID")) == "abc-123"

    def test_merge_exception_preserves_the_vtimezone_and_master(self) -> None:
        """calendar/intake.py's own path for an incoming REQUEST about one
        occurrence -- the VTIMEZONE and the master (and its own EXDATE)
        must survive a merge same as they survive a direct edit."""
        replacement = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
            "UID:exotic-1@example.com\r\n"
            "RECURRENCE-ID;TZID=Europe/Berlin:20260921T090000\r\n"
            "DTSTAMP:20260901T120000Z\r\n"
            "DTSTART;TZID=Europe/Berlin:20260921T140000\r\n"
            "DTEND;TZID=Europe/Berlin:20260921T150000\r\n"
            "SUMMARY:Weekly standup (moved once more)\r\n"
            "SEQUENCE:2\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        merged = ical.merge_exception(_EXOTIC_RECURRING_EVENT, replacement, "20260921T090000")

        assert len(_vtimezones(merged)) == 1
        master, exceptions = ical.parse_master_and_exceptions(merged)
        assert master.summary == "Weekly standup"
        assert "EXDATE" in _master_component(merged)
        assert len(exceptions) == 1
        assert exceptions[0].summary == "Weekly standup (moved once more)"
