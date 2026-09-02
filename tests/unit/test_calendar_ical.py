"""calendar/ical.py -- parsing, building and editing VEVENT bodies. No
database and no network: every case here is a plain string in, string or
dataclass out."""

from __future__ import annotations

from datetime import datetime, timezone

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


class TestMethodAndScheduleAgent:
    def test_strip_method_removes_it(self) -> None:
        stripped = ical.strip_method(_INVITATION_REQUEST)
        assert ical.get_method(stripped) is None
        # The event body itself survives intact.
        master, _ = ical.parse_master_and_exceptions(stripped)
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
