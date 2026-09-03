"""
Calendar UI actions against a real Radicale server -- the event editor's own
edit path, which the API and e2e layers do not exercise since neither drives
the actual form. Every action here goes through a control a person clicks,
and the assertion that an edit landed reads the real server back through
api_client, never a mock.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

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

        # fill() on a datetime-local input sets the DOM value without
        # React ever seeing it -- go through the input's own native
        # setter and fire the event React listens for instead.
        starts_input, ends_input = page.locator('input[type="datetime-local"]').all()
        for locator, value in (
            (starts_input, "2026-09-10T10:00"), (ends_input, "2026-09-10T11:00"),
        ):
            locator.evaluate(
                "(el, value) => {"
                "  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')"
                "    .set.call(el, value);"
                "  el.dispatchEvent(new Event('input', { bubbles: true }));"
                "}",
                value,
            )

        page.get_by_role("button", name="Save", exact=True).click()
        expect(page.get_by_text("Event created")).to_be_visible(timeout=10_000)

        def _created() -> dict[str, Any] | None:
            listed = api_client.get("/api/calendar/events", params={"month": "2026-09"}).json()
            return next((e for e in listed["events"] if e["summary"] == summary), None)

        event = wait_for(_created, description="Created event synced back")
        assert event["tz"] == browser_tz


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
