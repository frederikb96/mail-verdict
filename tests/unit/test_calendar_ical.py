"""calendar/ical.py -- parsing, building and editing VEVENT bodies. No
database and no network: every case here is a plain string in, string or
dataclass out."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

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
