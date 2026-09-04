"""
Calendar UI actions against a real Radicale server -- the event editor's own
edit path, which the API and e2e layers do not exercise since neither drives
the actual form. Every action here goes through a control a person clicks,
and the assertion that an edit landed reads the real server back through
api_client, never a mock.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.helpers import (
    unique_email,
    wait_for,
    wait_for_dav_account_active,
    wait_for_dav_collection,
    wait_for_event_synced,
    wait_for_mailpit_message,
)
from tests.setup.dav_helpers import create_calendar, discover
from tests.ui.helpers import (
    center_in_grid_viewport,
    drag_by_pixels,
    event_chip,
    event_occurrence_chip,
    set_date_input,
    wait_for_account_active,
)

from tests.setup.containers import (  # isort: skip
    DOVECOT_ALIAS,
    DOVECOT_IMAP_PORT,
    DOVECOT_PASSWORD,
    MAILPIT_ALIAS,
    MAILPIT_SMTP_PORT,
    RADICALE_ALIAS,
    RADICALE_PORT,
)

# Mirrors HOUR_HEIGHT in ui/src/components/calendar/time-grid.tsx -- the
# pixel math for a pointer drag in the time grid has no other source of
# truth to read it from.
_HOUR_HEIGHT_PX = 56


@pytest.fixture(scope="module")
def radicale_base_url(radicale_endpoint: tuple[str, int]) -> str:
    host, port = radicale_endpoint
    return f"http://{host}:{port}/"


@pytest.fixture(scope="module")
def ui_calendar_owner(radicale_base_url: str) -> str:
    """A calendar on the real Radicale server, owned by a fresh principal --
    the dav_account below points at it. Returns the username."""
    username = f"cal-{uuid.uuid4().hex[:8]}"
    with httpx.Client(auth=(username, "unused"), timeout=10.0) as client:
        principal = discover(client, radicale_base_url)
        create_calendar(client, principal, "work", "Work")
    return username


@pytest.fixture(scope="module")
def dav_account(api_client: httpx.Client, ui_calendar_owner: str) -> dict[str, Any]:
    resp = api_client.post(
        "/api/dav-accounts",
        json={
            "name": f"Radicale-{uuid.uuid4().hex[:8]}",
            "discovery_url": f"http://{RADICALE_ALIAS}:{RADICALE_PORT}/",
            "username": ui_calendar_owner,
            "password": "unused",
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_dav_account_active(api_client, account["id"])
    return account


@pytest.fixture(scope="module")
def calendar_collection(api_client: httpx.Client, dav_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_dav_collection(api_client, dav_account["id"], "Work")



@pytest.fixture(scope="module")
def organizer_identity(
    api_client: httpx.Client,
    dovecot_endpoint: tuple[str, int, int],
    calendar_collection: dict[str, Any],
) -> dict[str, Any]:
    """A real mail identity, linked to the shared calendar as its
    organiser -- an event created there with attendees is organised under
    this identity's own address, and deleting or editing it goes out over
    this identity's outbox."""
    _host, _imap_port, _lmtp_port = dovecot_endpoint
    email = unique_email("organizer")

    resp = api_client.post(
        "/api/accounts",
        json={
            "name": email,
            "imap_host": DOVECOT_ALIAS,
            "imap_port": DOVECOT_IMAP_PORT,
            "imap_user": email,
            "imap_password": DOVECOT_PASSWORD,
            "smtp_host": MAILPIT_ALIAS,
            "smtp_port": MAILPIT_SMTP_PORT,
            "smtp_user": email,
            "smtp_password": "unused",  # Mailpit accepts any SMTP AUTH credentials
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_account_active(api_client, account["id"])

    resp = api_client.post("/api/identities", json={"account_id": account["id"], "address": email})
    assert resp.status_code == 201, resp.text
    identity = resp.json()

    resp = api_client.patch(
        f"/api/calendars/{calendar_collection['id']}", json={"identity_id": identity["id"]},
    )
    assert resp.status_code == 200, resp.text

    identity["email"] = email
    return identity


class TestCalendarUi:
    """Shares one DAV account and its one synced calendar."""

    def test_renaming_an_event_from_the_editor_succeeds(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: the editor always sent an attendees
        array on every update, and the backend refuses any attendees value
        outright -- so this rename returned 422 before the editor stopped
        sending the field it cannot change anyway. Named to avoid the word
        "edit" in the event summary, which would otherwise collide with the
        Edit button's own accessible name under substring matching."""
        original_summary = f"Standup {uuid.uuid4()}"
        resp = api_client.post(
            "/api/calendar/events",
            json={
                "calendar_id": calendar_collection["id"],
                "summary": original_summary,
                "dtstart": "2026-09-15T10:00:00Z",
                "dtend": "2026-09-15T11:00:00Z",
            },
        )
        assert resp.status_code == 201, resp.text
        created = wait_for_event_synced(api_client, resp.json()["object_id"])

        new_summary = f"Renamed {uuid.uuid4()}"

        page.goto(f"{app_server}/calendar")
        page.get_by_role("tab", name="Agenda").click()
        chip = event_chip(page, created["object_id"])
        expect(chip).to_be_visible(timeout=15_000)
        chip.click()

        page.get_by_role("button", name="Edit", exact=True).click()
        title_input = page.get_by_label("Title")
        expect(title_input).to_be_visible(timeout=15_000)
        title_input.fill(new_summary)
        page.get_by_role("button", name="Save", exact=True).click()

        expect(page.get_by_text("Event updated")).to_be_visible(timeout=10_000)

        def _renamed() -> dict[str, Any] | None:
            detail = api_client.get(f"/api/calendar/events/{created['object_id']}").json()
            return detail if detail["summary"] == new_summary else None

        wait_for(_renamed, description="Renamed event synced back")

    def test_a_click_on_a_grid_chip_does_not_write(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: the drag hook committed a move on
        every pointerdown/pointerup pair, with no check that the release
        landed anywhere other than where the press started -- so a plain
        click in the day/week grid bumped the event's sequence and
        truncated its stored seconds, and would mail an "Updated:" notice
        to every guest of an organized event on every click."""
        summary = f"Grid click test {uuid.uuid4()}"
        dtstart = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        resp = api_client.post(
            "/api/calendar/events",
            json={
                "calendar_id": calendar_collection["id"],
                "summary": summary,
                "dtstart": dtstart.isoformat(),
                "dtend": (dtstart + timedelta(hours=1)).isoformat(),
            },
        )
        assert resp.status_code == 201, resp.text
        created = wait_for_event_synced(api_client, resp.json()["object_id"])

        page.goto(f"{app_server}/calendar")
        chip = event_chip(page, created["object_id"])
        expect(chip).to_be_visible(timeout=15_000)
        chip.click()

        # A click still does something the user expects: it opens the
        # popover for the event that was clicked. Scoped to a paragraph --
        # the chip itself is a <span> carrying this same text.
        expect(page.get_by_role("paragraph").filter(has_text=summary)).to_be_visible(timeout=10_000)
        # ... and does not do the thing it must not: no toast, no write.
        page.wait_for_timeout(1_500)
        expect(page.get_by_text("Event moved")).not_to_be_visible()

        after = api_client.get(f"/api/calendar/events/{created['object_id']}").json()
        assert after["sequence"] == created["sequence"]
        assert after["dtstart"] == created["dtstart"]

    def test_dragging_one_occurrence_of_a_series_asks_which_scope(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: dragging one occurrence rewrote the
        whole series' DTSTART with no scope prompt -- every occurrence
        jumped to the dragged time and the one that was actually dragged
        disappeared from where it started."""
        summary = f"Grid series drag test {uuid.uuid4()}"
        dtstart = datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0)
        month = dtstart.strftime("%Y-%m")
        resp = api_client.post(
            "/api/calendar/events",
            json={
                "calendar_id": calendar_collection["id"],
                "summary": summary,
                "dtstart": dtstart.isoformat(),
                "dtend": (dtstart + timedelta(minutes=30)).isoformat(),
                "rrule": "FREQ=WEEKLY;COUNT=3",
            },
        )
        assert resp.status_code == 201, resp.text
        object_id = resp.json()["object_id"]
        wait_for_event_synced(api_client, object_id)

        def _three_occurrences() -> list[dict[str, Any]] | None:
            listed = api_client.get(
                "/api/calendar/events",
                params={"month": month, "calendars": calendar_collection["id"]},
            )
            assert listed.status_code == 200, listed.text
            matches = [e for e in listed.json()["events"] if e["summary"] == summary]
            return matches if len(matches) == 3 else None

        occurrences = wait_for(_three_occurrences, description="Three occurrences expanded")
        first, second, third = sorted(occurrences, key=lambda e: e["dtstart"])

        page.goto(f"{app_server}/calendar")
        chip = event_occurrence_chip(page, object_id, first["recurrence_id"])
        expect(chip).to_be_visible(timeout=15_000)
        center_in_grid_viewport(page, chip)
        box = chip.bounding_box()
        assert box is not None, "occurrence chip has no bounding box after centering it"
        start_x = box["x"] + box["width"] / 2
        start_y = box["y"] + box["height"] / 2

        drag_by_pixels(page, start_x, start_y, start_x, start_y + 3 * _HOUR_HEIGHT_PX)

        expect(page.get_by_text("Change recurring event")).to_be_visible(timeout=10_000)
        page.get_by_role("button", name="This event", exact=True).click()
        expect(page.get_by_text("Event moved")).to_be_visible(timeout=10_000)

        def _dragged_occurrence_moved() -> dict[str, Any] | None:
            listed = api_client.get(
                "/api/calendar/events",
                params={"month": month, "calendars": calendar_collection["id"]},
            ).json()["events"]
            current = next(
                (e for e in listed if e["object_id"] == object_id
                 and e["recurrence_id"] == first["recurrence_id"]),
                None,
            )
            return current if current and current["dtstart"] != first["dtstart"] else None

        moved = wait_for(_dragged_occurrence_moved, description="Dragged occurrence moved")
        assert moved["is_exception"] is True

        final = api_client.get(
            "/api/calendar/events", params={"month": month, "calendars": calendar_collection["id"]},
        ).json()["events"]
        remaining = {e["recurrence_id"]: e["dtstart"] for e in final if e["object_id"] == object_id}
        # The occurrence that was not touched must read exactly as the
        # series originally produced it -- proof the master's own DTSTART
        # and RRULE were left alone, not silently shifted along with the
        # one occurrence that was dragged.
        assert remaining[second["recurrence_id"]] == second["dtstart"]
        assert remaining[third["recurrence_id"]] == third["dtstart"]

    def test_create_by_drag_on_empty_grid_space_opens_the_editor(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: the grid surface compared the
        pointerdown target against its own div by strict identity, so a
        press on one of its hour-line children -- everywhere but the
        surface's own four edges -- never started a create-drag at all.
        And had it fired, it created an untitled event with no editor to
        fill in before it reached the server."""
        page.goto(f"{app_server}/calendar")
        # A week far enough out that no other test's events share it.
        next_button = page.get_by_role("button", name="Next", exact=True)
        for _ in range(4):
            next_button.click()

        page.evaluate(
            "document.querySelector('[data-testid=\"time-grid-scroll\"]').scrollTop = 0"
        )
        column = page.locator("[data-grid-surface]").first
        expect(column).to_be_visible(timeout=10_000)
        date_str = column.get_attribute("data-date")
        assert date_str is not None
        month = date_str[:7]

        before = api_client.get(
            "/api/calendar/events", params={"month": month, "calendars": calendar_collection["id"]},
        ).json()["events"]

        box = column.bounding_box()
        assert box is not None
        x = box["x"] + box["width"] / 2
        # 03:00 -- clear of the default 08:00 scroll-to-hour on a week that
        # isn't today's, comfortably inside the viewport once scrolled to 0.
        start_y = box["y"] + 3 * _HOUR_HEIGHT_PX
        drag_by_pixels(page, x, start_y, x, start_y + _HOUR_HEIGHT_PX / 2)

        # Nothing is created until Save -- the editor opens instead.
        title_input = page.get_by_label("Title")
        expect(title_input).to_be_visible(timeout=10_000)
        mid = api_client.get(
            "/api/calendar/events", params={"month": month, "calendars": calendar_collection["id"]},
        ).json()["events"]
        assert len(mid) == len(before)

        summary = f"Grid create-by-drag test {uuid.uuid4()}"
        title_input.fill(summary)
        page.get_by_role("button", name="Save", exact=True).click()
        expect(page.get_by_text("Event created")).to_be_visible(timeout=10_000)

        def _created() -> dict[str, Any] | None:
            listed = api_client.get(
                "/api/calendar/events",
                params={"month": month, "calendars": calendar_collection["id"]},
            ).json()["events"]
            return next((e for e in listed if e["summary"] == summary), None)

        wait_for(_created, description="Event created via the drag-opened editor")

    def test_create_by_drag_prefills_the_dragged_range_not_a_rounded_hour(
        self, page: Page, app_server: str, calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: the grid computed the real dragged
        start and length, then discarded both and passed only the bare date
        onward -- so the editor's default range rounded unconditionally to
        the next full hour with a fixed one-hour length regardless of what
        was actually dragged. A drag from 3am to 5am must open the editor
        at 3am to 5am, not 4am to 5am."""
        page.goto(f"{app_server}/calendar")
        next_button = page.get_by_role("button", name="Next", exact=True)
        for _ in range(5):
            next_button.click()

        page.evaluate(
            "document.querySelector('[data-testid=\"time-grid-scroll\"]').scrollTop = 0"
        )
        column = page.locator("[data-grid-surface]").first
        expect(column).to_be_visible(timeout=10_000)
        date_str = column.get_attribute("data-date")
        assert date_str is not None

        box = column.bounding_box()
        assert box is not None
        x = box["x"] + box["width"] / 2
        start_y = box["y"] + 3 * _HOUR_HEIGHT_PX
        drag_by_pixels(page, x, start_y, x, start_y + 2 * _HOUR_HEIGHT_PX)

        title_input = page.get_by_label("Title")
        expect(title_input).to_be_visible(timeout=10_000)

        starts_input, ends_input = page.locator('input[type="datetime-local"]').all()
        assert starts_input.input_value() == f"{date_str}T03:00"
        assert ends_input.input_value() == f"{date_str}T05:00"

    def test_creating_a_timed_event_binds_the_browsers_own_timezone(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: the editor never sent `tz` on
        create, so a timed event lost its named zone regardless of what
        the API itself could do with one -- the API's own binding
        behaviour for a given zone (kept wall-clock reading, moved
        instant) is proven directly in
        TestCreateWithTimezone in the pg layer; this only has to show the
        editor actually sends the field at all. Pinning the browser to a
        specific zone via `browser_context_args`/the marker was tried and
        dropped: this app's SSR renders with the server's own (host) zone,
        so a client pinned to a different one hydrates mismatched
        (React error #418) and the toolbar's click handlers go missing --
        a real bug, but not one this row owns. Reading the browser's own
        zone back keeps the assertion meaningful without touching it."""
        summary = f"DST check {uuid.uuid4()}"

        page.goto(f"{app_server}/calendar")
        browser_tz = page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
        # The editor seeds its Calendar field from useCalendars() at the
        # moment it mounts and never re-reads it once that query resolves
        # later -- opening it before the sidebar's own calendar list has
        # loaded leaves Save permanently disabled.
        expect(page.get_by_role("checkbox", name="Work")).to_be_visible(timeout=15_000)

        page.get_by_role("button", name="New event").click()
        title_input = page.get_by_label("Title")
        expect(title_input).to_be_visible(timeout=15_000)
        title_input.fill(summary)

        starts_input, ends_input = page.locator('input[type="datetime-local"]').all()
        set_date_input(starts_input, "2026-09-10T10:00")
        set_date_input(ends_input, "2026-09-10T11:00")

        page.get_by_role("button", name="Save", exact=True).click()
        expect(page.get_by_text("Event created")).to_be_visible(timeout=10_000)

        def _created() -> dict[str, Any] | None:
            listed = api_client.get("/api/calendar/events", params={"month": "2026-09"}).json()
            return next((e for e in listed["events"] if e["summary"] == summary), None)

        event = wait_for(_created, description="Created event synced back")
        assert event["tz"] == browser_tz

    def test_a_timed_event_is_stored_at_the_wall_clock_that_was_entered(
        self,
        browser: Browser,
        app_server: str,
        api_client: httpx.Client,
        calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: the editor converted its input to a
        UTC instant *and* sent the browser's zone alongside it, and the API
        keeps the reading it is given and binds it to that zone -- so the
        UTC reading was stamped Europe/Berlin and every event created in
        the interface landed one whole offset early. Editing was correct,
        because no zone is sent on an edit, so only creating was wrong.

        The zone has to be pinned and has to be a non-UTC one: on a UTC
        browser the two readings are the same number and the defect is
        invisible, which is how it survived the pass that introduced it.
        The test above proves the zone travels at all; this one proves
        what it is bound to."""
        summary = f"Wall clock {uuid.uuid4()}"
        context = browser.new_context(timezone_id="Europe/Berlin")
        page = context.new_page()
        try:
            page.goto(f"{app_server}/calendar")
            # Same race as the test above: the Calendar field is seeded
            # from useCalendars() once, at mount.
            expect(page.get_by_role("checkbox", name="Work")).to_be_visible(timeout=15_000)

            page.get_by_role("button", name="New event", exact=True).click()
            title_input = page.get_by_label("Title")
            expect(title_input).to_be_visible(timeout=15_000)
            title_input.fill(summary)

            starts_input, ends_input = page.locator('input[type="datetime-local"]').all()
            set_date_input(starts_input, "2026-09-10T10:00")
            set_date_input(ends_input, "2026-09-10T11:00")

            page.get_by_role("button", name="Save", exact=True).click()
            expect(page.get_by_text("Event created")).to_be_visible(timeout=10_000)
        finally:
            context.close()

        def _created() -> dict[str, Any] | None:
            listed = api_client.get("/api/calendar/events", params={"month": "2026-09"}).json()
            return next((e for e in listed["events"] if e["summary"] == summary), None)

        event = wait_for(_created, description="Created event synced back")
        assert event["tz"] == "Europe/Berlin"
        stored = datetime.fromisoformat(event["dtstart"])
        assert (stored.hour, stored.minute) == (10, 0), (
            f"entered 10:00 in Europe/Berlin, stored {event['dtstart']}"
        )
        assert stored.utcoffset() == timedelta(hours=2)

    def test_retyping_the_year_in_the_editor_does_not_take_the_page_down(
        self, page: Page, app_server: str, calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: one digit typed into the Starts
        field's year segment replaced the whole calendar with the error
        boundary's screen, editor and typed values gone. Two causes, both
        needed: an unpadded year rendered back as `2-09-03T06:00`, which
        the control cannot parse and so empties itself; and the state
        updater read `e.target.value` from the live element inside its own
        body, which React runs during render -- so the empty value was
        read a render later and `new Date("").toISOString()` threw where an
        error boundary could catch it.

        Retyping a date is what a person does, not an edge case. The
        arrow-key walk is how the year segment is reached: a
        datetime-local renders as month/day/year/hour/minute segments, and
        ArrowLeft/ArrowRight move between them."""
        page.goto(f"{app_server}/calendar")
        expect(page.get_by_role("checkbox", name="Work")).to_be_visible(timeout=15_000)

        page.get_by_role("button", name="New event", exact=True).click()
        expect(page.get_by_label("Title")).to_be_visible(timeout=15_000)

        starts_input = page.locator('input[type="datetime-local"]').first
        original = starts_input.input_value()
        starts_input.click()
        for _ in range(6):
            page.keyboard.press("ArrowLeft")
        page.keyboard.press("ArrowRight")
        page.keyboard.press("ArrowRight")
        page.keyboard.type("2")

        expect(page.get_by_text("Something went wrong")).to_have_count(0)
        expect(page.get_by_label("Title")).to_be_visible()
        expect(starts_input).to_have_value(f"0002{original[4:]}")

    def test_a_popover_whose_event_cannot_be_loaded_says_so(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: the popover treated "not loaded
        yet" and "did not load" as one state, so a failed detail fetch
        left it spinning forever. A spinner that never stops reads as a
        hang rather than as an error, which is how a broken fetch stays
        unnoticed until someone times how long they have been waiting."""
        summary = f"Fetch failure {uuid.uuid4()}"
        created = api_client.post(
            "/api/calendar/events",
            json={
                "calendar_id": calendar_collection["id"], "summary": summary,
                "dtstart": "2026-09-24T10:00:00+00:00", "dtend": "2026-09-24T11:00:00+00:00",
            },
        )
        assert created.status_code == 201, created.text
        object_id = created.json()["object_id"]

        # The detail route only -- the month list has no path segment
        # after "events", so it still answers normally and the chip
        # renders.
        page.route(
            "**/api/calendar/events/*",
            lambda route: route.fulfill(
                status=503, content_type="application/json",
                body='{"detail": "Calendar server unavailable"}',
            ),
        )
        page.goto(f"{app_server}/calendar")
        page.get_by_role("tab", name="Month", exact=True).click()

        chip = event_chip(page, object_id)
        expect(chip).to_be_visible(timeout=15_000)
        chip.click()

        expect(page.get_by_text("This event could not be loaded", exact=False)).to_be_visible(
            timeout=10_000
        )
        expect(page.get_by_text("Calendar server unavailable", exact=False)).to_be_visible()

    def test_creating_an_all_day_event_stores_the_exclusive_end(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: the editor sent the same day for
        both ends of an all-day event and the API stored it verbatim, so
        DTSTART == DTEND -- a zero-length event RFC 5545 forbids (DTEND is
        exclusive), and one a UTC or western browser's month/week views
        hid entirely (endCol < startCol). Runs on this suite's own host
        clock, which is UTC -- the zone the bug was invisible in."""
        summary = f"All day test {uuid.uuid4()}"

        page.goto(f"{app_server}/calendar")
        # Same race as the timed-create test above: the Calendar field is
        # seeded from useCalendars() once, at mount.
        expect(page.get_by_role("checkbox", name="Work")).to_be_visible(timeout=15_000)

        page.get_by_role("button", name="New event").click()
        title_input = page.get_by_label("Title")
        expect(title_input).to_be_visible(timeout=15_000)
        title_input.fill(summary)

        page.get_by_role("switch", name="All day").click()

        date_inputs = page.locator('input[type="date"]')
        expect(date_inputs).to_have_count(2)
        start_value = date_inputs.nth(0).input_value()
        end_value = date_inputs.nth(1).input_value()
        assert start_value == end_value, (
            "the all-day fields must show the same day the timed default was "
            "showing, not a UTC day one off from a near-midnight local instant"
        )

        page.get_by_role("button", name="Save", exact=True).click()
        expect(page.get_by_text("Event created")).to_be_visible(timeout=10_000)

        month = datetime.now(timezone.utc).strftime("%Y-%m")

        def _created() -> dict[str, Any] | None:
            listed = api_client.get(
                "/api/calendar/events",
                params={"month": month, "calendars": calendar_collection["id"]},
            ).json()
            return next((e for e in listed["events"] if e["summary"] == summary), None)

        event = wait_for(_created, description="Created all-day event synced back")
        assert event["all_day"] is True
        dtstart = datetime.fromisoformat(event["dtstart"])
        dtend = datetime.fromisoformat(event["dtend"])
        assert dtend == dtstart + timedelta(days=1), (
            f"DTEND must be exactly one day after DTSTART (RFC 5545's exclusive "
            f"end), not the same instant -- got dtstart={dtstart!r} dtend={dtend!r}"
        )

        chip = event_chip(page, event["object_id"])
        expect(chip).to_be_visible(timeout=10_000)  # the week view's all-day tray

        page.get_by_role("tab", name="Month", exact=True).click()
        expect(chip).to_be_visible(timeout=10_000)  # a bar in the month grid

    def test_deleting_an_organised_event_with_guests_names_the_guest_count(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        mailpit_http_url: str,
        calendar_collection: dict[str, Any],
        organizer_identity: dict[str, Any],
    ) -> None:
        """The regression this guards: "am I the organiser" was implemented
        as "the event has no organiser", true only for a purely local
        event -- so this event, organised by the calendar's own linked
        identity, showed the generic "cannot be undone" warning while
        still silently mailing a cancellation to its guest. The dialog
        should name the guest count instead."""
        summary = f"Organised {uuid.uuid4()}"
        resp = api_client.post(
            "/api/calendar/events",
            json={
                "calendar_id": calendar_collection["id"],
                "summary": summary,
                "dtstart": "2026-09-16T10:00:00Z",
                "dtend": "2026-09-16T11:00:00Z",
                "attendees": [{"email": "guest@example.com"}],
            },
        )
        assert resp.status_code == 201, resp.text
        created = wait_for_event_synced(api_client, resp.json()["object_id"])
        assert created["organizer"] is not None
        assert created["organizer"]["email"].lower() == organizer_identity["email"].lower()

        page.goto(f"{app_server}/calendar")
        page.get_by_role("tab", name="Agenda", exact=True).click()
        chip = event_chip(page, created["object_id"])
        expect(chip).to_be_visible(timeout=15_000)
        chip.click()

        page.get_by_role("button", name="Delete", exact=True).click()
        expect(
            page.get_by_text(
                "A cancellation will be sent to 1 guest. This cannot be undone.", exact=True,
            )
        ).to_be_visible(timeout=10_000)
        page.get_by_role("button", name="Delete permanently", exact=True).click()

        expect(chip).to_be_hidden(timeout=10_000)
        wait_for_mailpit_message(mailpit_http_url, f"Cancelled: {summary}")

    def test_choosing_a_dropdown_option_in_the_editor_leaves_it_open(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: the popover dismissed itself on any
        press outside its own DOM subtree, exempting only the Sheet and
        dialog popups by name -- and a Select renders its options into a
        portal of its own, which matched neither. So choosing a calendar or
        a repeat unmounted the popover and the editor it renders with it,
        before Save could be reached at all."""
        summary = f"Dropdown reach {uuid.uuid4()}"
        resp = api_client.post(
            "/api/calendar/events",
            json={
                "calendar_id": calendar_collection["id"],
                "summary": summary,
                "dtstart": "2026-09-17T10:00:00Z",
                "dtend": "2026-09-17T11:00:00Z",
            },
        )
        assert resp.status_code == 201, resp.text
        created = wait_for_event_synced(api_client, resp.json()["object_id"])

        page.goto(f"{app_server}/calendar")
        page.get_by_role("tab", name="Agenda", exact=True).click()
        chip = event_chip(page, created["object_id"])
        expect(chip).to_be_visible(timeout=15_000)
        chip.click()
        page.get_by_role("button", name="Edit", exact=True).click()

        sheet = page.locator('[data-slot="sheet-content"]')
        expect(sheet).to_be_visible(timeout=15_000)
        # Calendar first, Repeats second -- the editor's own field order.
        calendar_select, repeat_select = sheet.locator('[data-slot="select-trigger"]').all()

        calendar_select.click()
        page.get_by_role("option", name="Work", exact=True).click()
        expect(sheet).to_be_visible()

        repeat_select.click()
        page.get_by_role("option", name="Weekly", exact=True).click()
        expect(sheet).to_be_visible()

        page.get_by_role("button", name="Save", exact=True).click()
        expect(page.get_by_text("Event updated")).to_be_visible(timeout=10_000)

        def _repeats_weekly() -> dict[str, Any] | None:
            detail = api_client.get(f"/api/calendar/events/{created['object_id']}").json()
            return detail if (detail["rrule"] or "").startswith("FREQ=WEEKLY") else None

        wait_for(_repeats_weekly, description="Repeat chosen through the editor saved")

    def test_the_editors_calendar_and_repeat_controls_show_labels(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: both controls rendered the raw value
        behind them -- the calendar's uuid and the event's own RRULE text --
        because the underlying control only resolves a label itself when it
        is given an item list, which nothing in this application passes."""
        summary = f"Label check {uuid.uuid4()}"
        resp = api_client.post(
            "/api/calendar/events",
            json={
                "calendar_id": calendar_collection["id"],
                "summary": summary,
                "dtstart": "2026-09-18T10:00:00Z",
                "dtend": "2026-09-18T11:00:00Z",
                "rrule": "FREQ=WEEKLY;COUNT=2",
            },
        )
        assert resp.status_code == 201, resp.text
        object_id = resp.json()["object_id"]
        created = wait_for_event_synced(api_client, object_id)

        page.goto(f"{app_server}/calendar")
        page.get_by_role("tab", name="Agenda", exact=True).click()
        chip = event_chip(page, created["object_id"]).first
        expect(chip).to_be_visible(timeout=15_000)
        chip.click()
        page.get_by_role("button", name="Edit", exact=True).click()

        sheet = page.locator('[data-slot="sheet-content"]')
        expect(sheet).to_be_visible(timeout=15_000)
        expect(sheet.get_by_text("Work", exact=True)).to_be_visible(timeout=10_000)
        expect(sheet.get_by_text("Weekly", exact=True)).to_be_visible(timeout=10_000)
        expect(sheet.get_by_text(calendar_collection["id"], exact=False)).to_have_count(0)
        expect(sheet.get_by_text("FREQ=", exact=False)).to_have_count(0)

    def test_editing_an_organised_event_names_the_guests_it_will_mail(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        calendar_collection: dict[str, Any],
        organizer_identity: dict[str, Any],
    ) -> None:
        """The regression this guards: the editor asked whether the event
        had an organiser at all rather than whether that organiser is the
        viewer, so an event organised by the calendar's own identity got
        the generic warning on delete -- and saving an edit mailed every
        guest an update with no prompt whatsoever."""
        summary = f"Organised in the sheet {uuid.uuid4()}"
        resp = api_client.post(
            "/api/calendar/events",
            json={
                "calendar_id": calendar_collection["id"],
                "summary": summary,
                "dtstart": "2026-09-19T10:00:00Z",
                "dtend": "2026-09-19T11:00:00Z",
                "attendees": [{"email": "guest@example.com"}],
            },
        )
        assert resp.status_code == 201, resp.text
        created = wait_for_event_synced(api_client, resp.json()["object_id"])
        assert created["organizer"] is not None

        page.goto(f"{app_server}/calendar")
        page.get_by_role("tab", name="Agenda", exact=True).click()
        chip = event_chip(page, created["object_id"])
        expect(chip).to_be_visible(timeout=15_000)
        chip.click()
        page.get_by_role("button", name="Edit", exact=True).click()

        sheet = page.locator('[data-slot="sheet-content"]')
        expect(sheet).to_be_visible(timeout=15_000)

        # Delete first, then back out of it -- the confirmation has to name
        # the guest the cancellation actually goes to.
        sheet.get_by_role("button", name="Delete", exact=True).click()
        expect(
            page.get_by_text(
                "A cancellation will be sent to 1 guest. This cannot be undone.", exact=True,
            )
        ).to_be_visible(timeout=10_000)
        page.locator('[data-slot="dialog-content"]').get_by_role(
            "button", name="Cancel", exact=True,
        ).click()

        title_input = page.get_by_label("Title")
        title_input.fill(f"{summary} revised")
        page.get_by_role("button", name="Save", exact=True).click()
        expect(
            page.get_by_text("An update will be sent to 1 guest.", exact=True)
        ).to_be_visible(timeout=10_000)
        page.get_by_role("button", name="Save and notify", exact=True).click()
        expect(page.get_by_text("Event updated")).to_be_visible(timeout=10_000)

    def test_a_grid_drag_does_not_open_the_popover_it_would_fill_with_stale_values(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: beginning a move captures the pointer
        on the chip, and a captured pointer retargets the click the browser
        derives from that press-and-release back to the capturing element
        wherever the pointer physically ends up. So every drag also fired the
        chip's own click handler, opening the popover on values the move had
        just replaced."""
        summary = f"Grid drag popover {uuid.uuid4()}"
        dtstart = datetime.now(timezone.utc).replace(hour=8, minute=0, second=0, microsecond=0)
        resp = api_client.post(
            "/api/calendar/events",
            json={
                "calendar_id": calendar_collection["id"],
                "summary": summary,
                "dtstart": dtstart.isoformat(),
                "dtend": (dtstart + timedelta(minutes=30)).isoformat(),
            },
        )
        assert resp.status_code == 201, resp.text
        created = wait_for_event_synced(api_client, resp.json()["object_id"])

        page.goto(f"{app_server}/calendar")
        chip = event_chip(page, created["object_id"])
        expect(chip).to_be_visible(timeout=15_000)
        center_in_grid_viewport(page, chip)
        box = chip.bounding_box()
        assert box is not None, "chip has no bounding box after centering it"
        start_x = box["x"] + box["width"] / 2
        start_y = box["y"] + box["height"] / 2

        drag_by_pixels(page, start_x, start_y, start_x, start_y + 2 * _HOUR_HEIGHT_PX)

        expect(page.get_by_text("Event moved")).to_be_visible(timeout=10_000)
        # The popover is what a click opens, and a drag is not a click.
        expect(page.get_by_role("button", name="Edit", exact=True)).to_have_count(0)

    def test_new_event_opened_before_the_calendars_arrive_can_still_be_saved(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: the editor read its default calendar
        out of a query in a state initialiser and in an effect keyed on the
        sheet opening, neither of which runs again when that query resolves
        afterwards. Opening New event first left the calendar empty and Save
        disabled for good, with nothing said about why.

        The calendars response is held open in the page rather than raced
        against: holding it is the same condition a slow network produces,
        and it is the only way to be sure the editor really did open first."""
        # Holds the first calendar list request until the test releases it.
        page.add_init_script(
            """
            (() => {
              const original = window.fetch;
              window.__releaseCalendars = null;
              window.fetch = function (input, init) {
                const target = typeof input === "string" ? input : input.url;
                if (/\\/api\\/calendars(\\?|$)/.test(target) && !window.__releaseCalendars) {
                  return new Promise((resolve, reject) => {
                    window.__releaseCalendars = () =>
                      original(input, init).then(resolve, reject);
                  });
                }
                return original(input, init);
              };
            })();
            """
        )

        summary = f"Late calendars {uuid.uuid4()}"
        page.goto(f"{app_server}/calendar")
        page.get_by_role("button", name="New event", exact=True).click()

        title_input = page.get_by_label("Title")
        expect(title_input).to_be_visible(timeout=15_000)
        title_input.fill(summary)

        sheet = page.locator('[data-slot="sheet-content"]')
        save = sheet.get_by_role("button", name="Save", exact=True)
        expect(save).to_be_disabled()
        assert page.evaluate("() => !!window.__releaseCalendars"), (
            "the calendar list was never requested -- nothing was held back"
        )

        page.evaluate("() => window.__releaseCalendars()")

        expect(save).to_be_enabled(timeout=15_000)
        save.click()
        expect(page.get_by_text("Event created")).to_be_visible(timeout=10_000)

    def test_a_browser_in_another_timezone_hydrates_without_error(
        self, browser: Browser, app_server: str,
    ) -> None:
        """The regression this guards: every page is prerendered to static
        HTML at build time, so the calendar's anchor date, today's column
        and the current-time line were all baked from the build machine's
        own clock. A browser on a different day hydrated against markup for
        another one, React reported the mismatch and rebuilt the tree.

        Two zones 25 hours apart are never on the same date as each other,
        so whatever zone the build ran in, at least one of them differs from
        it -- the check does not depend on where this runs."""
        for timezone_id in ("Pacific/Kiritimati", "Pacific/Midway"):
            context = browser.new_context(timezone_id=timezone_id)
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            try:
                page.goto(f"{app_server}/calendar")
                expect(page.get_by_test_id("calendar-toolbar-title")).to_be_visible(
                    timeout=15_000
                )
                # Handlers survive: the toolbar still responds to a click.
                page.get_by_role("button", name="New event", exact=True).click()
                expect(page.get_by_label("Title")).to_be_visible(timeout=15_000)
            finally:
                context.close()
            assert errors == [], f"{timezone_id}: {errors}"

    def test_manage_calendars_selects_show_labels_not_raw_ids(
        self,
        page: Page,
        app_server: str,
        dav_account: dict[str, Any],
        calendar_collection: dict[str, Any],
        organizer_identity: dict[str, Any],
    ) -> None:
        """The regression this guards: every Select in this dialog rendered
        its raw stored value -- a UUID for the identity and the server, the
        internal enum member for the invitations intake -- instead of a
        label a person can read."""
        page.goto(f"{app_server}/calendar")
        page.get_by_role("button", name="Manage calendars", exact=True).click()

        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=15_000)
        expect(dialog.get_by_text(organizer_identity["email"], exact=True)).to_be_visible(
            timeout=10_000
        )
        expect(dialog.get_by_text("Do nothing with invitations", exact=True)).to_be_visible(
            timeout=10_000
        )
        expect(dialog.get_by_text(dav_account["name"], exact=True)).to_be_visible(timeout=10_000)

        expect(dialog.get_by_text(calendar_collection["id"], exact=False)).to_have_count(0)
        expect(dialog.get_by_text(organizer_identity["id"], exact=False)).to_have_count(0)
        expect(dialog.get_by_text(dav_account["id"], exact=False)).to_have_count(0)
        expect(dialog.get_by_text("none", exact=True)).to_have_count(0)

    def test_unchecking_a_calendar_hides_its_events_immediately(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: the checkbox wrote is_visible and
        nothing told the separately-cached events query to refetch under
        the new visibility, so the calendar's events kept rendering
        exactly as before -- the toggle looked entirely inert. Restores
        visibility at the end so later tests sharing this module's
        calendar still see it."""
        summary = f"Visibility toggle test {uuid.uuid4()}"
        resp = api_client.post(
            "/api/calendar/events",
            json={
                "calendar_id": calendar_collection["id"], "summary": summary,
                "dtstart": "2026-09-20T10:00:00Z", "dtend": "2026-09-20T11:00:00Z",
            },
        )
        assert resp.status_code == 201, resp.text
        created = wait_for_event_synced(api_client, resp.json()["object_id"])

        try:
            page.goto(f"{app_server}/calendar")
            page.get_by_role("tab", name="Month", exact=True).click()
            chip = event_chip(page, created["object_id"])
            expect(chip).to_be_visible(timeout=15_000)

            checkbox = page.get_by_role("checkbox", name="Work", exact=True)
            checkbox.click()
            with pytest.raises(AssertionError):
                expect(chip).to_be_visible(timeout=20_000)

            checkbox.click()
            expect(chip).to_be_visible(timeout=10_000)
        finally:
            api_client.patch(
                f"/api/calendars/{calendar_collection['id']}", json={"is_visible": True},
            )

    def test_disabling_a_calendar_in_the_manage_dialog_hides_it_everywhere(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dav_account: dict[str, Any],
    ) -> None:
        """The regression this guards: there was only one visibility level
        -- a calendar could be toggled per-view or not at all, with no way
        to declutter it out of the sidebar and the event editor's Calendar
        picker entirely. A calendar created just for this test, so the
        module's shared "Work" calendar is never touched."""
        name = f"Disable test {uuid.uuid4().hex[:8]}"
        created = api_client.post(
            "/api/calendars", json={"dav_account_id": dav_account["id"], "display_name": name},
        )
        assert created.status_code == 201, created.text

        page.goto(f"{app_server}/calendar")
        checkbox = page.get_by_role("checkbox", name=name, exact=True)
        expect(checkbox).to_be_visible(timeout=15_000)

        page.get_by_role("button", name="Manage calendars", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=15_000)
        dialog.get_by_role("switch", name=f"Show {name} in the sidebar", exact=True).click()
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden(timeout=10_000)

        with pytest.raises(AssertionError):
            expect(checkbox).to_be_visible(timeout=8_000)

        # The same calendars query backs the event editor's own Calendar
        # picker -- the sidebar checkbox already having loaded (above) is
        # what proves that query has resolved before the dropdown opens.
        page.get_by_role("button", name="New event", exact=True).click()
        sheet = page.locator('[data-slot="sheet-content"]')
        expect(sheet).to_be_visible(timeout=15_000)
        sheet.locator('[data-slot="select-trigger"]').first.click()
        expect(page.get_by_role("option", name=name, exact=True)).to_have_count(0)

    def test_clicking_a_yearly_all_day_event_shows_its_popover_promptly(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: a birthday-shaped occurrence (an
        all-day yearly series, fetched by its own recurrence-id the way a
        month-view click always does) opened an empty popover with a
        spinner that never resolved. The actual cause was never anything
        specific to this shape of event -- it was the same shared-event-
        loop stall the month view's own perf fix (elsewhere in this
        codebase) already resolves, so this proves the popover survives
        that fix rather than adding a second one."""
        summary = f"Birthday-shaped {uuid.uuid4()}"
        resp = api_client.post(
            "/api/calendar/events",
            json={
                "calendar_id": calendar_collection["id"], "summary": summary,
                "dtstart": "2026-09-22T00:00:00Z", "dtend": "2026-09-23T00:00:00Z",
                "all_day": True, "rrule": "FREQ=YEARLY",
            },
        )
        assert resp.status_code == 201, resp.text
        object_id = resp.json()["object_id"]
        wait_for_event_synced(api_client, object_id)

        page.goto(f"{app_server}/calendar")
        # Month, not Agenda -- the agenda list virtualizes its rows and this
        # date can land outside the initial render margin once enough other
        # tests' events occupy the nearer ones; the month grid renders the
        # whole month regardless.
        page.get_by_role("tab", name="Month", exact=True).click()
        chip = event_chip(page, object_id)
        expect(chip).to_be_visible(timeout=15_000)
        chip.click()

        expect(page.get_by_role("paragraph").filter(has_text=summary)).to_be_visible(timeout=10_000)
        expect(page.get_by_text("This event could not be loaded", exact=False)).to_have_count(0)

    def test_a_forced_create_failure_leaves_the_editor_open_with_data_intact(
        self, page: Page, app_server: str, calendar_collection: dict[str, Any],
    ) -> None:
        """A save that cannot reach the server must say so rather than
        spin forever, and must leave whatever was typed in place -- the
        request is intercepted rather than driven through a genuine
        server error so the failure mode is exact and repeatable."""
        page.route(
            "**/api/calendar/events",
            lambda route: route.fulfill(
                status=503, content_type="application/json",
                body='{"detail": "Calendar server unavailable"}',
            ) if route.request.method == "POST" else route.continue_(),
        )
        page.goto(f"{app_server}/calendar")
        expect(page.get_by_role("checkbox", name="Work")).to_be_visible(timeout=15_000)

        summary = f"Forced failure {uuid.uuid4()}"
        page.get_by_role("button", name="New event", exact=True).click()
        title_input = page.get_by_label("Title")
        expect(title_input).to_be_visible(timeout=15_000)
        title_input.fill(summary)

        save = page.get_by_role("button", name="Save", exact=True)
        save.click()

        expect(
            page.get_by_text("Could not create event", exact=False)
        ).to_be_visible(timeout=10_000)
        # The spinner an in-flight mutation shows must be gone once it has
        # settled, whatever the outcome -- and the entered data survives.
        expect(save.locator(".animate-spin")).to_have_count(0)
        expect(title_input).to_have_value(summary)
        expect(title_input).to_be_visible()

    def test_today_after_navigating_away_agrees_across_toolbar_grid_and_mini_month(
        self, page: Page, app_server: str,
    ) -> None:
        """The regression this guards: the month scroller's own scroll
        listener wrote the Monday of whatever week was passing under the
        top of the viewport into calendarDateAtom while Today's
        programmatic smooth-scroll was still animating, so Today could
        land on a different week's Monday instead of today's own weekday
        -- and the toolbar and the grid's own header, each computing the
        current month separately, could disagree about which month that
        even was. The mini-month, seeded once from the anchor, never
        followed navigation at all."""
        page.goto(f"{app_server}/calendar")

        page.get_by_role("tab", name="Day", exact=True).click()
        today_title = page.get_by_test_id("calendar-toolbar-title").text_content()
        assert today_title

        page.get_by_role("tab", name="Month", exact=True).click()
        initial_month = page.get_by_test_id("calendar-toolbar-title").text_content()
        assert initial_month

        for _ in range(3):
            page.get_by_role("button", name="Next", exact=True).click()
        page.get_by_role("button", name="Today", exact=True).click()

        toolbar_title = page.get_by_test_id("calendar-toolbar-title")
        expect(toolbar_title).to_have_text(initial_month, timeout=10_000)
        expect(page.get_by_test_id("month-grid-title")).to_have_text(initial_month, timeout=10_000)
        expect(page.get_by_test_id("mini-month-title")).to_have_text(initial_month, timeout=10_000)

        page.get_by_role("tab", name="Day", exact=True).click()
        expect(toolbar_title).to_have_text(today_title, timeout=10_000)


class TestCalendarNavigation:
    """View, date, scroll position and zoom -- none of this needs a
    synced calendar, so it runs against a bare app_server rather than
    sharing TestCalendarUi's DAV fixtures."""

    def test_switching_views_updates_the_url_and_survives_the_back_button(
        self, page: Page, app_server: str,
    ) -> None:
        """The regression this guards: view and date lived in a plain
        in-memory atom with no URL of their own, so a reload always
        landed back on the default view and the browser's back button
        did nothing -- switching from Week into Day left no trace to
        return from."""
        page.goto(f"{app_server}/calendar")
        expect(page).to_have_url(re.compile(r"[?&]view=week(&|$)"))

        page.get_by_role("tab", name="Day", exact=True).click()
        expect(page).to_have_url(re.compile(r"[?&]view=day(&|$)"))
        expect(page.get_by_role("tab", name="Day", exact=True)).to_have_attribute(
            "aria-selected", "true",
        )

        page.go_back()
        expect(page).to_have_url(re.compile(r"[?&]view=week(&|$)"))
        expect(page.get_by_role("tab", name="Week", exact=True)).to_have_attribute(
            "aria-selected", "true",
        )

    def test_a_day_reached_from_the_month_view_can_be_left_by_going_back(
        self, page: Page, app_server: str,
    ) -> None:
        """The concrete case named for this feature: jumping from the
        month view into a specific day, then going back, must return to
        the month view rather than landing somewhere else entirely."""
        page.goto(f"{app_server}/calendar")
        page.get_by_role("tab", name="Month", exact=True).click()
        expect(page).to_have_url(re.compile(r"[?&]view=month(&|$)"))

        # Today's own cell, not just "the first day-cell button": the
        # month grid is a virtualized, continuously scrolling list still
        # settling its render window right after a mount, and .first
        # picks up whatever week currently sits first in DOM order --
        # liable to be unmounted and replaced mid-click. Today's date is
        # always inside the render window the anchor date opens with.
        today_iso = page.evaluate(
            "() => { const d = new Date(); "
            "return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') "
            "+ '-' + String(d.getDate()).padStart(2, '0'); }"
        )
        day_cell = page.locator(f'button[data-date="{today_iso}"]')
        expect(day_cell).to_be_visible(timeout=15_000)
        day_cell.click()
        expect(page).to_have_url(re.compile(r"[?&]view=day(&|$)"))

        page.go_back()
        expect(page).to_have_url(re.compile(r"[?&]view=month(&|$)"))

    def test_month_year_picker_jumps_to_a_chosen_month(
        self, page: Page, app_server: str,
    ) -> None:
        """Clicking the header title opens a month grid for the current
        year first; clicking the year switches to a year grid."""
        page.goto(f"{app_server}/calendar")
        page.get_by_role("tab", name="Month", exact=True).click()

        current_year = page.evaluate("new Date().getFullYear()")
        target_year = current_year - 1

        page.get_by_test_id("calendar-toolbar-title").click()
        page.get_by_role("button", name=str(current_year), exact=True).click()
        page.get_by_role("button", name=str(target_year), exact=True).click()
        page.get_by_role("button", name="Mar", exact=True).click()

        expect(page.get_by_test_id("calendar-toolbar-title")).to_have_text(
            f"March {target_year}", timeout=10_000,
        )

    def test_time_grid_scroll_position_persists_across_view_changes(
        self, page: Page, app_server: str,
    ) -> None:
        """The regression this guards: the day/week grid always scrolled
        to a fixed 08:00 (or an hour before now) on every mount, so
        leaving a view and coming back -- even switching from week to
        day on the same date -- lost exactly where the reader was."""
        page.goto(f"{app_server}/calendar")
        page.get_by_role("tab", name="Day", exact=True).click()
        scroller = page.locator('[data-testid="time-grid-scroll"]')
        expect(scroller).to_be_visible(timeout=15_000)

        scroller.evaluate("(el) => { el.scrollTop = 500; }")
        # Past the debounce that keeps the persisted write off every
        # scroll frame.
        page.wait_for_timeout(600)

        page.get_by_role("tab", name="Month", exact=True).click()
        page.get_by_role("tab", name="Week", exact=True).click()

        restored = page.locator('[data-testid="time-grid-scroll"]')
        expect(restored).to_be_visible(timeout=15_000)
        scroll_top = restored.evaluate("(el) => el.scrollTop")
        assert abs(scroll_top - 500) < 5, f"expected scrollTop near 500, got {scroll_top}"

    def test_ctrl_wheel_zooms_the_time_grid_and_the_zoom_persists(
        self, page: Page, app_server: str,
    ) -> None:
        page.goto(f"{app_server}/calendar")
        page.get_by_role("tab", name="Day", exact=True).click()
        scroller = page.locator('[data-testid="time-grid-scroll"]')
        expect(scroller).to_be_visible(timeout=15_000)

        box = scroller.bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.keyboard.down("Control")
        page.mouse.wheel(0, -600)
        page.keyboard.up("Control")
        page.wait_for_timeout(300)

        stored = page.evaluate("() => localStorage.getItem('mailverdict:calendar-zoom')")
        assert stored is not None, "zoom was never persisted"
        assert json.loads(stored) > 1.0, f"expected zoom > 1.0 after zooming in, got {stored}"
